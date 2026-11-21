"""ask-rebuild's window: an agent asks to switch the system, and the user answers.

Full screen over everything (a layer-shell overlay on every edge that takes the keyboard; a
fullscreen window where layer-shell isn't there), so the request interrupts. It shows the agent's
reason, what the switch changes (the closure diff), the commits since the running system, the
uncommitted files and the rebuild args, with a message box for the user's answer, Deny and Approve.
Prints {"decision": "approve"|"deny"|"timeout", "message": "..."} and exits; exits 1 without an answer
when there is no display. The job closes it (SIGTERM) when the user answered on another device first.

The buttons wake up after ARM_S, so keys typed into another window at the moment it opens can't
answer; those land in the message box instead. Escape denies; Ctrl+Enter approves. Closing it
denies; no answer within the request's timeout is "timeout".
"""
import json
import os
import sys
import time

from . import text
from .panel import CSS as PANEL_CSS, PALETTE, LayerShell, box, label  # first: it picks the GTK versions
from .text import diff_summary
from .util import read_json
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402, I001

ARM_S = 1.5
CSS = b"""
window.um-ask { background: rgba(10, 10, 12, 0.84); }
#ask-card { background: @menu_bg; color: @menu_fg; border-radius: 18px; border: 1px solid @menu_border;
            padding: 26px 30px 22px 30px; }
.um-ask .ask-title { font-weight: 800; font-size: 24px; }
.um-ask .ask-reason { font-size: 17px; }
.um-ask .ask-note { color: #f9e27d; font-size: 13px; }
.um-ask entry { min-height: 34px; font-size: 14px; }
.um-ask button.big { min-height: 36px; padding: 4px 22px; font-size: 14px; font-weight: 700; }
.um-ask button.deny { background: alpha(#e01b24, 0.7); color: white; }
"""


class Approve(Gtk.Application):
    def __init__(self, request):
        super().__init__(application_id=None, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.request = request
        self.answered = False
        self.armed = False
        self.deadline = time.monotonic() + int(request.get('timeout') or 900)
        self.connect('activate', self.build)

    def build(self, *_):
        display = Gdk.Display.get_default()
        if display is None:  # no graphical session (functor's headless desktop): ask-rebuild asks elsewhere
            print('update-manager: no display to show the window on', file=sys.stderr, flush=True)
            self.quit()
            return
        css = Gtk.CssProvider()
        theme = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
                             'waybar/menu-theme.css')
        try:
            with open(theme, 'rb') as f:
                overrides = f.read()
        except OSError:
            overrides = b''
        css.load_from_data(PALETTE + overrides + PANEL_CSS + CSS)
        Gtk.StyleContext.add_provider_for_display(display, css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property('gtk-application-prefer-dark-theme', True)
        window = self.window = Gtk.ApplicationWindow(application=self, title='Approve rebuild')
        window.add_css_class('um')
        window.add_css_class('um-ask')
        window.set_decorated(False)
        if LayerShell is not None and LayerShell.is_supported():
            LayerShell.init_for_window(window)
            LayerShell.set_namespace(window, 'update-manager-ask')
            LayerShell.set_layer(window, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(window, LayerShell.KeyboardMode.EXCLUSIVE)
            LayerShell.set_exclusive_zone(window, -1)  # over the bar too
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(window, edge, True)
        else:
            window.fullscreen()
        window.connect('close-request', lambda *_: self.answer('deny') or False)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.key)
        window.add_controller(keys)
        window.set_child(self.card())
        window.present()
        self.message.grab_focus()
        GLib.timeout_add(int(ARM_S * 1000), self.arm)
        GLib.timeout_add(1000, self.tick)
        self.tick()

    def card(self):
        r = self.request
        card = box(spacing=12, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        card.set_name('ask-card')
        card.set_size_request(900, -1)
        card.append(label(f"Switch {r.get('host') or 'this machine'} to a new build?", 'ask-title'))
        source = 'An agent asks' + (f" · from {r['from']}" if r.get('from') else '')
        card.append(label(source, ['muted', 'small'], wrap=True))
        card.append(label(r.get('reason') or '', 'ask-reason', wrap=True, selectable=True))
        if r.get('note'):
            card.append(label(r['note'], 'ask-note', wrap=True))

        details = box(spacing=6)
        details.append(label('WHAT CHANGES', 'section'))
        if r.get('diff_error'):
            details.append(label(f"Could not compare the closures: {r['diff_error']}", 'problem', wrap=True))
        diff = r.get('diff')
        details.append(label(diff_summary(diff) if diff is not None else '', 'small'))
        for change in (diff or {}).get('changes', []):
            row = box(False, 8, margin_start=12)
            row.append(label(change['name'], 'mono', ellipsize=True, hexpand=True))
            row.append(label(text.change_text(change), ['mono', 'muted'], ellipsize=True))
            details.append(row)
        if diff and diff['count'] > len(diff['changes']):
            details.append(label(f"and {diff['count'] - len(diff['changes'])} more", ['muted', 'small'],
                                 margin_start=12))

        details.append(label('SOURCE', 'section'))
        commits = r.get('commits')
        if commits is None:
            details.append(label("The running system's commit is unknown.", ['muted', 'small']))
        elif not commits:
            details.append(label('No new commits since the running system.', ['muted', 'small']))
        else:
            details.append(label(f"{len(commits)} commit{'s' if len(commits) != 1 else ''} since the running "
                                 'system:', 'small'))
            for c in commits:
                row = box(False, 8, margin_start=12)
                row.append(label(c['rev'][:8], 'hash'))
                row.append(label(c['subject'], 'commit', ellipsize=True, hexpand=True))
                details.append(row)
        files = r.get('uncommitted') or []
        if files:
            details.append(label(f"{len(files)} uncommitted file{'s' if len(files) != 1 else ''}:", 'small'))
            details.append(label('\n'.join(files), ['mono', 'muted'], margin_start=12, selectable=True))
        if r.get('args'):
            details.append(label('Rebuild args: ' + ' '.join(r['args']), 'mono', wrap=True, selectable=True))
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=380)
        scroller.set_child(details)
        card.append(scroller)

        self.message = Gtk.Entry(placeholder_text='Message to the agent (optional)')
        card.append(self.message)
        row = box(False, 10)
        self.countdown = label('', ['muted', 'small'], hexpand=True, valign=Gtk.Align.CENTER)
        row.append(self.countdown)
        self.deny = Gtk.Button(label='Deny (Esc)', sensitive=False)
        self.deny.add_css_class('big')
        self.deny.add_css_class('deny')
        self.deny.connect('clicked', lambda _b: self.answer('deny'))
        row.append(self.deny)
        self.approve = Gtk.Button(label='Approve and switch', sensitive=False)
        self.approve.add_css_class('big')
        self.approve.add_css_class('suggested-action')
        self.approve.set_tooltip_text('Ctrl+Enter')
        self.approve.connect('clicked', lambda _b: self.answer('approve'))
        row.append(self.approve)
        card.append(row)
        return card

    def arm(self):
        self.armed = True
        self.deny.set_sensitive(True)
        self.approve.set_sensitive(True)
        return False

    def tick(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            self.answer('timeout')
            return False
        minutes, seconds = divmod(int(left), 60)
        self.countdown.set_text(f'No answer in {minutes}:{seconds:02d} counts as no.')
        return True

    def key(self, _controller, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.answer('deny')
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            if self.armed:
                self.answer('approve')
            return True
        return False

    def answer(self, decision):
        if self.answered:
            return
        self.answered = True
        print(json.dumps({'decision': decision, 'message': self.message.get_text().strip()}), flush=True)
        self.quit()


def main(argv):
    os.environ['LD_PRELOAD'] = os.environ.pop('UPDATE_MANAGER_PRELOADED', os.environ.get('LD_PRELOAD', ''))
    if not os.environ['LD_PRELOAD']:
        del os.environ['LD_PRELOAD']
    request = read_json(argv[0])
    if not isinstance(request, dict):
        print(f'update-manager: no request in {argv[0]}', file=sys.stderr)
        return 2
    app = Approve(request)
    app.run([])
    return 0 if app.answered else 1
