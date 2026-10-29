"""The panel: a layer-shell popup under the bar, kept loaded and hidden so it opens at once.

It reads only state.json (watching it for changes) and runs actions through
`update-manager request HOST ACTION`, in the background, so nothing here waits
on the network or Nix.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:  # Optional: without it (or a compositor that has it) the panel is a normal window.
    gi.require_version('Gtk4LayerShell', '1.0')
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

from . import collect, config, text  # noqa: E402
from .util import ago_text, clock_text, duration_text, size_text  # noqa: E402

CONFIRM_S = 4  # A button that changes a system asks once more for this long.
EXPAND = bool(os.environ.get('UPDATE_MANAGER_PANEL_EXPAND'))  # every expander open (screenshots)
PALETTE = b"""
@define-color menu_bg #222226;
@define-color menu_card #343437;
@define-color menu_hover #414144;
@define-color menu_accent #3584e4;
@define-color menu_fg #ffffff;
@define-color menu_muted rgba(255, 255, 255, 0.68);
@define-color menu_border rgba(255, 255, 255, 0.06);
"""
CSS = b"""
window.um, window.um > * { background: transparent; }
#um-card { background: @menu_bg; color: @menu_fg; border-radius: 14px; border: 1px solid @menu_border;
           padding: 14px 14px 12px 14px; }
.um .title { font-weight: 800; font-size: 15px; }
.um .muted { color: @menu_muted; }
.um .small { font-size: 12px; }
.um .mono { font-family: monospace; font-size: 11.5px; }
.um .card { background: @menu_card; border-radius: 10px; padding: 10px 12px; }
.um .card-title { font-weight: 700; font-size: 14px; }
.um .section { font-weight: 700; font-size: 11px; letter-spacing: 1px; color: @menu_muted; margin-top: 4px; }
.um .pill { border-radius: 999px; padding: 1px 9px; font-size: 11.5px; font-weight: 700; }
.um .pill.current { background: alpha(#57e389, 0.18); color: #8ff0a4; }
.um .pill.updates { background: alpha(#f6d32d, 0.18); color: #f9e27d; }
.um .pill.busy { background: alpha(#3584e4, 0.25); color: #99c1f1; }
.um .pill.reboot { background: alpha(#ff7800, 0.22); color: #ffbe6f; }
.um .pill.offline, .um .pill.unknown { background: alpha(#ffffff, 0.08); color: @menu_muted; }
.um .pill.failed, .um .pill.conflicts, .um .pill.error { background: alpha(#e01b24, 0.22); color: #f66151; }
.um .pill.clean { background: alpha(#3584e4, 0.22); color: #99c1f1; }
.um .tag { border-radius: 6px; padding: 0 6px; font-size: 11px; background: alpha(#ffffff, 0.07);
           color: @menu_muted; }
.um .tag.stale { background: alpha(#f6d32d, 0.14); color: #f9e27d; }
.um button { padding: 3px 10px; min-height: 26px; border-radius: 7px; }
.um button.suggested-action { background: @menu_accent; color: white; }
.um button:disabled { opacity: 0.45; }
.um button.suggested-action:disabled { background: alpha(@menu_accent, 0.35); }
.um button.confirm { background: #c64600; color: white; }
.um .commit { font-size: 12px; }
.um .hash { font-family: monospace; font-size: 11.5px; color: #99c1f1; }
.um progressbar trough { min-height: 6px; border-radius: 3px; }
.um progressbar progress { min-height: 6px; border-radius: 3px; background: @menu_accent; }
.um progressbar text { font-size: 11px; color: @menu_muted; }
.um expander title { font-size: 12px; color: @menu_muted; }
.um .problem { color: #f66151; font-size: 12px; }
.um separator { background: @menu_border; }
.um stackswitcher button { padding: 2px 12px; }
"""


def label(text_='', css=(), wrap=False, xalign=0.0, markup=False, ellipsize=False, selectable=False, **kw):
    widget = Gtk.Label(xalign=xalign, **kw)
    widget.set_markup(text_) if markup else widget.set_text(text_ or '')
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_natural_wrap_mode(Gtk.NaturalWrapMode.NONE)
    if ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    if selectable:
        widget.set_selectable(True)
    for c in ([css] if isinstance(css, str) else css):
        widget.add_css_class(c)
    return widget


def box(vertical=True, spacing=6, css=(), **kw):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                     spacing=spacing, **kw)
    for c in ([css] if isinstance(css, str) else css):
        widget.add_css_class(c)
    return widget


def esc(value):
    return GLib.markup_escape_text(str(value if value is not None else ''))


def pill(words, kind):
    return label(words, ['pill', kind], valign=Gtk.Align.CENTER)


def clear(container):
    child = container.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        container.remove(child)
        child = nxt


class Panel(Adw.Application):
    def __init__(self):
        super().__init__(application_id=config.APP_ID, flags=Gio.ApplicationFlags.IS_SERVICE)
        self.window = None
        self.state = None
        self.focused_once = False
        self.pending = {}  # button key -> deadline, for the confirm-once buttons
        self.busy_keys = set()  # actions sent, waiting for an answer
        self.tick_source = None
        self.connect('startup', self.startup)
        self.connect('activate', lambda *_: self.toggle())

    # --- window -----------------------------------------------------------------------
    def startup(self, *_):
        self.hold()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        for name, kind in (('toggle', None), ('show', GLib.VariantType.new('s'))):
            action = Gio.SimpleAction.new(name, kind)
            action.connect('activate', self.toggle_action if name == 'toggle' else self.show_page)
            self.add_action(action)
        if os.environ.get('UPDATE_MANAGER_PANEL_DEBUG'):
            # Driving the panel without a pointer (headless tests): click a button or expander by its
            # label, scroll the page to a fraction.
            for name, handler in (('debug-click', self.debug_click), ('debug-scroll', self.debug_scroll)):
                action = Gio.SimpleAction.new(name, GLib.VariantType.new('s'))
                action.connect('activate', handler)
                self.add_action(action)
        css = Gtk.CssProvider()
        theme = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'waybar/menu-theme.css'
        try:
            overrides = theme.read_bytes()
        except OSError:
            overrides = b''
        css.load_from_data(PALETTE + overrides + CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.build()
        self.window.realize()
        self.monitor = Gio.File.new_for_path(str(collect.VIEW.parent)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect('changed', self.file_changed)

    def build(self):
        self.window = Adw.ApplicationWindow(application=self, title='Updates')
        self.window.add_css_class('um')
        self.window.connect('close-request', self.close)
        self.window.connect('notify::is-active', self.focus_changed)
        width = int(config.get('panel', 'width') or 600)
        if LayerShell is not None and LayerShell.is_supported():
            self.window.set_decorated(False)
            LayerShell.init_for_window(self.window)
            LayerShell.set_namespace(self.window, 'update-manager')
            LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
            keyboard = (LayerShell.KeyboardMode.NONE if os.environ.get('UPDATE_MANAGER_PANEL_PASSIVE')
                        else LayerShell.KeyboardMode.ON_DEMAND)
            LayerShell.set_keyboard_mode(self.window, keyboard)
            LayerShell.set_anchor(self.window, LayerShell.Edge.TOP, True)
            LayerShell.set_anchor(self.window, LayerShell.Edge.RIGHT, True)
            LayerShell.set_margin(self.window, LayerShell.Edge.TOP, int(config.get('panel', 'margin') or 0) + 4)
            LayerShell.set_margin(self.window, LayerShell.Edge.RIGHT,
                                  int(config.get('panel', 'margin_right') or 150))
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key)
        self.window.add_controller(keys)
        engaged = Gtk.EventControllerMotion()
        engaged.connect('enter', lambda *_: setattr(self, 'focused_once', True))
        self.window.add_controller(engaged)

        card = box(spacing=10)
        card.set_name('um-card')
        card.set_size_request(width, -1)
        header = box(False, 10)
        header.append(label('', ['title'], valign=Gtk.Align.CENTER))
        header.append(label('Updates', 'title', valign=Gtk.Align.CENTER, hexpand=True))
        self.headline = label('', ['muted', 'small'], wrap=True)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        switcher = Gtk.StackSwitcher(stack=self.stack, valign=Gtk.Align.CENTER)
        header.append(switcher)
        refresh = Gtk.Button.new_from_icon_name('view-refresh-symbolic')
        refresh.add_css_class('flat')
        refresh.set_tooltip_text('Check everything again now')
        refresh.connect('clicked', lambda _b: self.refresh_all())
        header.append(refresh)
        close = Gtk.Button.new_from_icon_name('window-close-symbolic')
        close.add_css_class('flat')
        close.connect('clicked', self.close)
        header.append(close)
        card.append(header)
        card.append(self.headline)
        self.problem = label('', 'problem', wrap=True, visible=False)
        card.append(self.problem)

        self.machines = box(spacing=10)
        self.upstream = box(spacing=10)
        for name, title, child in (('machines', 'Machines', self.machines), ('upstream', 'Upstream', self.upstream)):
            scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                          max_content_height=int(config.get('panel', 'height') or 500))
            scroller.set_child(child)
            self.stack.add_titled(scroller, name, title)
        card.append(self.stack)
        self.toasts = Adw.ToastOverlay(child=card)
        clamp = Adw.Clamp(maximum_size=width, tightening_threshold=width, child=self.toasts)
        self.window.set_content(clamp)
        self.load()

    def key(self, _controller, keyval, *_):
        if keyval == Gdk.KEY_Escape:
            return self.close()
        return False

    def focus_changed(self, *_):
        if self.window.is_active():
            self.focused_once = True
        elif self.focused_once and self.window.get_visible() and not os.environ.get('UPDATE_MANAGER_PANEL_STAY'):
            self.close()

    def close(self, *_):
        self.window.set_visible(False)
        if self.tick_source:
            GLib.source_remove(self.tick_source)
            self.tick_source = None
        return True

    def toggle_action(self, *_):
        self.toggle()

    def toggle(self):
        if self.window.get_visible():
            self.close()
            return
        self.focused_once = False
        self.load()
        self.window.present()
        self.tick_source = self.tick_source or GLib.timeout_add_seconds(5, self.tick)

    def show_page(self, _action, page):
        if self.stack.get_child_by_name(page.get_string()):
            self.stack.set_visible_child_name(page.get_string())
        if not self.window.get_visible():
            self.toggle()

    def widgets(self, root):
        child = root.get_first_child()
        while child is not None:
            yield child
            yield from self.widgets(child)
            child = child.get_next_sibling()

    def debug_click(self, _action, value):
        wanted = value.get_string()
        for widget in self.widgets(self.window):
            if not widget.get_mapped():
                continue
            if isinstance(widget, Gtk.Expander) and (widget.get_label() or '').startswith(wanted):
                widget.set_expanded(not widget.get_expanded())
                return
            if isinstance(widget, Gtk.Button) and widget.get_label() == wanted and widget.get_sensitive():
                widget.emit('clicked')
                return
            if isinstance(widget, Gtk.StackSwitcher):
                for button in self.widgets(widget):
                    if isinstance(button, Gtk.ToggleButton) and any(
                            isinstance(w, Gtk.Label) and w.get_label() == wanted for w in self.widgets(button)):
                        button.set_active(True)
                        return
        print(f'debug-click: nothing called {wanted!r}', file=sys.stderr)

    def debug_scroll(self, _action, value):
        scroller = self.stack.get_visible_child()
        adjustment = scroller.get_vadjustment()
        top = adjustment.get_upper() - adjustment.get_page_size()
        adjustment.set_value(max(0.0, top * float(value.get_string())))

    def tick(self):
        self.render()  # relative times ("seen 2 min ago")
        return True

    def file_changed(self, _monitor, file, other, event):
        # The agent replaces state.json by renaming a temporary file over it: that is RENAMED with
        # state.json as the *other* file.
        names = {f.get_basename() for f in (file, other) if f is not None}
        if collect.VIEW.name in names and event in (
                Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.RENAMED,
                Gio.FileMonitorEvent.MOVED_IN):
            GLib.timeout_add(100, lambda: self.load() and False)

    def load(self):
        try:
            self.state = collect.read_view()
        except Exception as e:  # noqa: BLE001
            self.problem.set_text(f'Could not read the state: {e}')
            self.problem.set_visible(True)
            return
        self.render()

    # --- actions ----------------------------------------------------------------------
    def action_button(self, words, host, action, args=None, confirm=None, suggested=False, sensitive=True,
                      hint=None):
        key = f'{host}:{action}:{json.dumps(args or {}, sort_keys=True)}'
        waiting = key in self.busy_keys
        asking = self.pending.get(key, 0) > time.time()
        button = Gtk.Button(label='Working…' if waiting else (confirm if asking else words))
        button.set_sensitive(sensitive and not waiting)
        if suggested and not asking:
            button.add_css_class('suggested-action')
        if asking:
            button.add_css_class('confirm')
        if hint:
            button.set_tooltip_text(hint)

        def clicked(_b):
            if confirm and self.pending.get(key, 0) <= time.time():
                self.pending[key] = time.time() + CONFIRM_S
                GLib.timeout_add_seconds(CONFIRM_S, lambda: self.render() and False)
                self.render()
                return
            self.pending.pop(key, None)
            self.run_action(key, host, action, args)
        button.connect('clicked', clicked)
        return button

    def run_action(self, key, host, action, args):
        self.busy_keys.add(key)
        self.render()
        command = [*config.self_command(), 'request', host, action, json.dumps(args or {})]

        def work():
            try:
                done = subprocess.run(command, capture_output=True, text=True, timeout=120)
                try:
                    reply = json.loads(done.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    reply = {'ok': False, 'message': (done.stderr or done.stdout or 'no answer').strip()[-300:]}
            except (OSError, subprocess.TimeoutExpired) as e:
                reply = {'ok': False, 'message': str(e)}
            GLib.idle_add(self.action_done, key, reply)
        threading.Thread(target=work, daemon=True).start()

    def action_done(self, key, reply):
        self.busy_keys.discard(key)
        message = reply.get('message') or ('done' if reply.get('ok') else 'failed')
        toast = Adw.Toast.new(message if reply.get('ok') else f'Failed: {message}')
        toast.set_timeout(6)
        self.toasts.add_toast(toast)
        subprocess.Popen([*config.self_command(), 'refresh'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.render()
        return False

    def refresh_all(self):
        state = self.state or {}
        subprocess.Popen([*config.self_command(), 'refresh'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        forks_host = config.get('forks_host')
        if forks_host in (state.get('hosts') or {}):
            self.run_action(f'{forks_host}:fork-check', forks_host, 'fork-check', {})

    # --- drawing ----------------------------------------------------------------------
    def render(self):
        state = self.state
        now = time.time()
        clear(self.machines)
        clear(self.upstream)
        if not state:
            self.headline.set_text('no state yet')
            self.machines.append(label('The agent has not written anything yet. Is update-manager-agent '
                                       'running?', 'muted', wrap=True))
            return True
        self.headline.set_text(self.headline_text(state, now))
        if state.get('kept') or now - state.get('updated', 0) > 120:
            self.problem.set_text(f"The agent is not running: this is what it last saw, "
                                  f"{ago_text(state.get('updated'), now)}.")
            self.problem.set_visible(True)
        else:
            self.problem.set_visible(False)
        for name, host in (state.get('hosts') or {}).items():
            self.machines.append(self.host_card(name, host, state, now))
        if state.get('ci'):
            self.machines.append(self.ci_card(state['ci'], state, now))
        self.upstream_page(state, now)
        return True

    def headline_text(self, state, now):
        s = state.get('summary') or {}
        bits = []
        if s.get('behind'):
            bits.append(f"{len(s['behind'])} machine{'s' if len(s['behind']) != 1 else ''} behind")
        else:
            bits.append('all machines current')
        if s.get('ci_running'):
            bits.append('CI building')
        if s.get('profile_updates'):
            bits.append(f"{s['profile_updates']} package updates")
        if s.get('forks_behind'):
            bits.append(f"{len(s['forks_behind'])} upstream changes")
        bits.append(f"updated {ago_text(state.get('updated'), now)}")
        return ' · '.join(bits)

    def host_card(self, name, host, state, now):
        card = box(spacing=6, css='card')
        top = box(False, 8)
        top.append(label(name, 'card-title'))
        if host.get('label'):
            top.append(label(host['label'], ['muted', 'small'], valign=Gtk.Align.BASELINE))
        top.append(Gtk.Box(hexpand=True))
        top.append(label(text.seen_text(host, state.get('me'), now), ['muted', 'small']))
        headline, kind = text.host_headline(name, host, now)
        top.append(pill(headline, kind))
        card.append(top)
        if host.get('source') == 'none':
            card.append(label(f"{name} hasn't reported yet. It sends its state when its agent next reaches "
                              f"{state.get('ci_host')}.", ['muted', 'small'], wrap=True))
            return card
        sysinfo = host.get('system') or {}
        update = host.get('update') or {}
        line = f"Running <span font_family='monospace'>{esc(sysinfo.get('rev') or '?')}</span>"
        if sysinfo.get('generation'):
            line += f" · generation {sysinfo['generation']}"
        if sysinfo.get('switched'):
            line += f" · switched {esc(clock_text(sysinfo['switched'], now))}"
        if sysinfo.get('kernel'):
            line += f" · Linux {esc(sysinfo['kernel'])}"
            if sysinfo.get('reboot_pending'):
                line += f" <span color='#ffbe6f'>(booted {esc(sysinfo.get('booted_kernel'))}: reboot to finish)</span>"
        card.append(label(line, 'small', markup=True, wrap=True))
        target = update.get('target') or {}
        if update.get('state') == 'available':
            card.append(label(f"CI build <span font_family='monospace'>{esc((target.get('rev') or '?')[:12])}</span>"
                              f" ready, built {esc(clock_text(target.get('built'), now))}", 'small', markup=True))
            commits = update.get('commits') or []
            if commits:
                lines = box(spacing=1, margin_start=6)
                for c in commits[:6]:
                    row = box(False, 6)
                    row.append(label(c['rev'][:8], 'hash'))
                    row.append(label(c['subject'], 'commit', ellipsize=True, hexpand=True))
                    lines.append(row)
                if len(commits) > 6:
                    lines.append(label(f'and {len(commits) - 6} more', ['muted', 'small']))
                card.append(lines)
            elif update.get('behind') is None:
                card.append(label("The commits in between aren't known (its system wasn't built by nixos-ci).",
                                  ['muted', 'small'], wrap=True))
            card.append(self.diff_widget(update))
            estimate = text.estimate_text(update)
            if estimate:
                on = update.get('estimated_on')
                card.append(label(estimate + (f' (estimated on {on})' if on else ''), 'small', wrap=True))
            if update.get('error'):
                card.append(label(update['error'], 'problem', wrap=True))
        card.append(self.host_actions(name, host, state))
        if update.get('state') == 'available' and not (host.get('capabilities') or {}).get('switch_units'):
            card.append(label('Switch and Boot need the root units from the nixos-conf update-manager branch; '
                              'Rebuild works without them.', ['muted', 'small'], wrap=True))
        if host.get('queued'):
            for request in host['queued']:
                card.append(label(f"Queued: {request['action']} (asked {ago_text(request.get('queued'), now)}; "
                                  f"runs when {name} next checks in)", ['muted', 'small'], wrap=True))
        for job in (host.get('jobs') or [])[:1]:
            card.append(self.job_widget(job, now))
        profiles = host.get('profiles')
        if profiles and profiles.get('items'):
            card.append(self.profiles_widget(name, profiles, now))
        checkout = host.get('checkout')
        if checkout:
            card.append(self.checkout_line(checkout, now))
        return card

    def diff_widget(self, update):
        diff = update.get('diff')
        if not diff:
            return label('Working out what changes…' if update.get('pending') else '', ['muted', 'small'])
        parts = []
        if diff.get('kernel'):
            parts.append(f"Linux {diff['kernel'].get('from') or '?'} → {diff['kernel']['to']}")
        parts.append(f"{diff['upgraded']} upgraded")
        if diff['added']:
            parts.append(f"{diff['added']} new")
        if diff['removed']:
            parts.append(f"{diff['removed']} removed")
        parts.append(('+' if diff['size_delta'] >= 0 else '') + size_text(diff['size_delta']))
        expander = Gtk.Expander(label='Changes: ' + ' · '.join(parts), expanded=EXPAND)
        rows = box(spacing=1, margin_start=12, margin_top=4)
        for change in diff['changes'][:30]:
            row = box(False, 8)
            row.append(label(change['name'], 'mono', ellipsize=True, hexpand=True))
            row.append(label(text.change_text(change), ['mono', 'muted']))
            rows.append(row)
        if diff['count'] > 30:
            rows.append(label(f"and {diff['count'] - 30} more", ['muted', 'small']))
        expander.set_child(rows)
        return expander

    def host_actions(self, name, host, state):
        row = box(False, 6, margin_top=2)
        update = host.get('update') or {}
        caps = host.get('capabilities') or {}
        target = (update.get('target') or {}).get('path')
        available = update.get('state') == 'available' and target
        units_hint = None if caps.get('switch_units') else (
            'Needs the update-manager root units on this machine: deploy the nixos-conf update-manager branch. '
            'Until then, "Pull & rebuild" works (it uses the passwordless `sudo rebuild`).')
        reboot_after = bool((update.get('diff') or {}).get('kernel'))
        row.append(self.action_button('Switch', name, 'switch', {'path': target}, confirm='Switch now?',
                                      suggested=not reboot_after, sensitive=bool(available and caps.get('switch_units')),
                                      hint=units_hint or "Switch to nixos-ci's build: downloads it from functor's "
                                                         "cache and activates it"))
        row.append(self.action_button('Boot', name, 'boot', {'path': target}, confirm='Boot into it next time?',
                                      suggested=reboot_after, sensitive=bool(available and caps.get('switch_units')),
                                      hint=units_hint or 'Make it the default boot entry without switching now '
                                                         '(for a new kernel)'))
        checkout = host.get('checkout') or {}
        pull = bool(checkout.get('behind'))
        row.append(self.action_button('Pull & rebuild' if pull else 'Rebuild', name, 'rebuild', {'pull': pull},
                                      confirm='Rebuild now?', sensitive=bool(caps.get('rebuild')),
                                      hint=f"git pull --ff-only in {checkout.get('path', '~/nixos-conf')}, then "
                                           "`sudo rebuild` (builds and switches from the checkout)"))
        return row

    def job_widget(self, job, now):
        widget = box(spacing=3)
        state = job.get('state')
        when = job.get('finished') or job.get('started') or job.get('created')
        head = box(False, 6)
        head.append(label(job.get('title') or job.get('kind'), 'small', ellipsize=True, hexpand=True))
        head.append(pill({'running': 'running', 'queued': 'queued', 'done': 'done', 'failed': 'failed',
                          'lost': 'lost'}.get(state, state or '?'),
                         {'running': 'busy', 'queued': 'busy', 'done': 'current'}.get(state, 'failed')))
        head.append(label(ago_text(when, now), ['muted', 'small']))
        widget.append(head)
        progress = job.get('progress')
        if state == 'running' and progress:
            widget.append(self.progress_bar(progress))
        if job.get('message') and state != 'running':
            widget.append(label(job['message'], ['muted', 'small'], wrap=True, selectable=True))
        elif job.get('tail'):
            widget.append(label(job['tail'], ['mono', 'muted'], ellipsize=True))
        return widget

    def progress_bar(self, progress):
        bar = Gtk.ProgressBar(show_text=True)
        builds = progress.get('builds') or {}
        by = progress.get('bytes') or {}
        bits, fraction = [], None
        if builds.get('expected'):
            fraction = builds.get('done', 0) / builds['expected']
            bits.append(f"{builds.get('done', 0)}/{builds['expected']} built")
        if by.get('expected'):
            if fraction is None:
                fraction = by.get('copied', 0) / by['expected']
            bits.append(f"{size_text(by.get('copied'))} of {size_text(by['expected'])}")
        eta = (progress.get('eta') or {}).get('seconds')
        if eta is not None:
            bits.append(f"~{duration_text(eta)} left")
        if fraction is None:
            bar.pulse()
        else:
            bar.set_fraction(max(0.0, min(1.0, fraction)))
        bar.set_text(' · '.join(bits) or 'working…')
        return bar

    def profiles_widget(self, name, profiles, now):
        items = profiles.get('items') or []
        updates = [i for i in items if i.get('update')]
        oldest = max((i.get('locked_age_days') or 0 for i in items), default=0)
        title = (f"Profile packages: {len(updates)} of {len(items)} have updates" if updates
                 else f"Profile packages: {len(items)}, all current")
        if oldest > 60:
            title += f" · oldest pinned {round(oldest / 30)} months ago"
        expander = Gtk.Expander(label=title, expanded=EXPAND)
        rows = box(spacing=3, margin_start=12, margin_top=4)
        for item in sorted(items, key=lambda i: (not i.get('update'), i['name'])):
            row = box(False, 8)
            row.append(label(item['name'], 'small', ellipsize=True, hexpand=True))
            info = item.get('installed') or ''
            if item.get('locked_age_days') is not None:
                info += f" · {round(item['locked_age_days'])} d old"
            est = item.get('estimate') or {}
            if item.get('update') and est.get('ok'):
                info += f" · {size_text(est.get('download'))}" + (f", {est['builds']} to build" if est.get('builds')
                                                                  else '')
            if item.get('error'):
                info = item['error'][:60]
            row.append(label(info, ['muted', 'small'], ellipsize=True))
            if item.get('update'):
                row.append(self.action_button('Upgrade', name, 'profile-upgrade', {'name': item['name']}))
            rows.append(row)
        for release in profiles.get('releases') or []:
            latest = release.get('latest')
            rows.append(label(f"{release['label']}: " + (f"newest is {latest} (installed {release.get('installed')})"
                                                         if latest else 'no release yet'), ['muted', 'small'],
                              wrap=True))
        if len(updates) > 1:
            rows.append(self.action_button(f'Upgrade all {len(updates)}', name, 'profile-upgrade', {'name': '--all'},
                                           confirm='Upgrade every package?'))
        rows.append(label(f"checked {ago_text(profiles.get('checked'), now)}", ['muted', 'small']))
        expander.set_child(rows)
        return expander

    def checkout_line(self, checkout, now):
        bits = [f"{checkout.get('path')} on {checkout.get('branch') or '?'}"]
        if checkout.get('ahead'):
            bits.append(f"{checkout['ahead']} not pushed")
        if checkout.get('behind'):
            bits.append(f"{checkout['behind']} to pull")
        if checkout.get('dirty'):
            bits.append(f"{checkout['dirty']} uncommitted")
        if checkout.get('fetched'):
            bits.append(f"fetched {ago_text(checkout['fetched'], now)}")
        return label('Checkout: ' + ' · '.join(bits), ['muted', 'small'], wrap=True)

    def ci_card(self, ci, state, now):
        ci_host = state.get('ci_host')
        card = box(spacing=6, css='card')
        top = box(False, 8)
        top.append(label('nixos-ci', 'card-title'))
        top.append(label(f'on {ci_host} · builds origin/main for every machine', ['muted', 'small'], hexpand=True,
                         ellipsize=True))
        run = ci.get('running')
        main = ci.get('main') or {}
        failed = [h for h, r in (main.get('hosts') or {}).items() if not r.get('ok')]
        top.append(pill('building' if run else ('failed' if failed else 'idle'),
                        'busy' if run else ('failed' if failed else 'current')))
        card.append(top)
        if run:
            what = (f"Building <span font_family='monospace'>{esc(run.get('rev12'))}</span> "
                    f"{esc(run.get('subject') or '')}" if run.get('phase') == 'building'
                    else 'Fetching and evaluating origin/main')
            card.append(label(what + f" · for {esc(duration_text(run.get('elapsed')))}", 'small', markup=True,
                              wrap=True))
            progress = dict(run.get('progress') or {})
            if run.get('eta_s') is not None:
                progress['eta'] = {'seconds': run['eta_s']}
            card.append(self.progress_bar(progress))
            for r in (progress.get('running') or [])[:4]:
                card.append(label(f"  {r['name']} · {duration_text(r['elapsed'])}" + (
                    f" · {r['phase']}" if r.get('phase') else ''), ['mono', 'muted'], ellipsize=True))
            if run.get('eta_quality') == 'typical':
                card.append(label('ETA from past run times (nixos-ci gives no progress until the nixlog hook is '
                                  'deployed)', ['muted', 'small'], wrap=True))
        hosts = main.get('hosts') or {}
        results = ', '.join(f"{h} {'ok' if r.get('ok') else 'FAILED'}" for h, r in hosts.items())
        card.append(label(
            f"Last build <span font_family='monospace'>{esc((main.get('rev') or '?')[:12])}</span>: {esc(results)}"
            f" · {esc(clock_text(main.get('built'), now))}"
            + (f" in {main['minutes']} min" if main.get('minutes') is not None else ''), 'small', markup=True,
            wrap=True))
        bits = []
        if ci.get('next_poll'):
            bits.append(f"next check of origin/main {clock_text(ci['next_poll'], now)}")
        if ci.get('typical_s'):
            bits.append(f"a typical build takes {duration_text(ci['typical_s'])}")
        update = ci.get('update')
        if update:
            ok = all(h.get('ok') for h in (update.get('hosts') or {}).values())
            bits.append(f"updated-inputs build {'ok' if ok else 'FAILED'} {clock_text(update.get('built'), now)}")
        card.append(label(' · '.join(bits).capitalize(), ['muted', 'small'], wrap=True))
        inputs = ci.get('inputs') or []
        if inputs:
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, column_spacing=4,
                               row_spacing=4, homogeneous=False)
            for item in sorted(inputs, key=lambda i: -(i.get('age_days') or 0)):
                age = item.get('age_days')
                words = f"{item['name']} {round(age)} d" if age is not None else item['name']
                if item.get('stale'):
                    words += ' ↑'
                tag = label(words, ['tag'] + (['stale'] if item.get('stale') else []))
                tag.set_tooltip_text(f"{item.get('repo')} {item.get('ref') or ''} @ {item.get('rev')}"
                                     + (f" · upstream at {item['latest']}" if item.get('stale') else ''))
                flow.append(tag)
            card.append(label('FLAKE INPUTS (age of the locked revision; ↑ newer upstream)', 'section'))
            card.append(flow)
        row = box(False, 6, margin_top=2)
        row.append(self.action_button('Build now', ci_host, 'ci-run', sensitive=not run,
                                      hint='nixos-ci run: build origin/main for every machine now'))
        row.append(self.action_button('Build with updated inputs', ci_host, 'ci-update', sensitive=not run,
                                      hint='nixos-ci update: nix flake update, then build every machine'))
        if update and update.get('lock'):
            row.append(self.action_button('Commit updated lock', ci_host, 'ci-apply-lock', confirm='Commit it?',
                                          hint="Copy the lock that built into the checkout's flake.lock and "
                                               'commit it (not pushed)'))
        if run:
            row.append(self.action_button('Stop', ci_host, 'ci-stop', confirm='Stop the build?'))
        card.append(row)
        history = [h for h in ci.get('history') or [] if h.get('seconds')][:5]
        if history:
            expander = Gtk.Expander(label='Recent builds', expanded=EXPAND)
            rows = box(spacing=1, margin_start=12, margin_top=4)
            for h in history:
                ok = all(h['hosts'].values()) if h['hosts'] else False
                rows.append(label(f"{clock_text(h['start'], now):>12}  {h['rev12'][:8]}  "
                                  f"{duration_text(h['seconds']):>7}  {'ok' if ok else 'FAILED':<6} {h['subject'][:48]}",
                                  ['mono', 'muted'], ellipsize=True))
            expander.set_child(rows)
            card.append(expander)
        return card

    def upstream_page(self, state, now):
        forks = state.get('forks') or {}
        forks_host = config.get('forks_host')
        head = box(False, 8)
        head.append(label(f"Forks and vendored code on {forks_host}, against their upstreams", ['muted', 'small'],
                          hexpand=True, wrap=True))
        head.append(label(f"checked {ago_text(forks.get('checked'), now)}", ['muted', 'small']))
        head.append(self.action_button('Check now', forks_host, 'fork-check', suggested=True,
                                       hint='git fetch every upstream and preview the merges'))
        self.upstream.append(head)
        if not forks.get('items'):
            self.upstream.append(label('No results yet.', ['muted', 'small']))
        for item in forks.get('items') or []:
            self.upstream.append(self.fork_card(item, forks_host, now))
        patches = state.get('patches') or {}
        if patches.get('items'):
            card = box(spacing=6, css='card')
            top = box(False, 8)
            top.append(label('Patched packages in nixos-conf', 'card-title', hexpand=True))
            top.append(self.action_button('Check', forks_host, 'patch-check',
                                          hint="Compare each patched package's version with upstream nixpkgs"))
            card.append(top)
            for p in patches['items']:
                row = box(False, 8)
                row.append(label(p['name'], 'small'))
                n = len(p.get('files') or [])
                if p.get('error'):
                    info, kind = p['error'][:80], 'error'
                elif p.get('moved'):
                    info, kind = f"{p.get('locked')} → {p.get('upstream')} upstream · {n} patches to re-check", 'updates'
                else:
                    info, kind = f"{p.get('locked')} · {n} patches · upstream has the same version", 'current'
                row.append(label(info, ['muted', 'small'], hexpand=True, ellipsize=True))
                row.append(pill({'updates': 'moved', 'current': 'current', 'error': 'error'}[kind], kind))
                if p.get('moved'):
                    row.append(self.action_button('Ask agent', forks_host, 'patch-agent', {'name': p['name']},
                                                  hint='Start a T3 thread that updates the input and rebases '
                                                       'the patches (or copy its brief)'))
                card.append(row)
            self.upstream.append(card)

    def fork_card(self, item, host, now):
        card = box(spacing=5, css='card')
        top = box(False, 8)
        top.append(label(item['name'], 'card-title'))
        top.append(label(item.get('repo', ''), ['muted', 'small'], hexpand=True, ellipsize=True))
        status = item.get('status')
        words, kind = {
            'current': ('up to date', 'current'), 'fast-forward': ('fast-forward', 'clean'),
            'clean-merge': ('merges cleanly', 'clean'), 'clean-rebase': ('rebases cleanly', 'clean'),
            'conflicts': (f"{item.get('conflict_count', '?')} files conflict", 'conflicts'),
            'error': ('error', 'error'), 'missing': ('missing', 'offline'),
        }.get(status, (status or '?', 'unknown'))
        top.append(pill(words, kind))
        card.append(top)
        if item.get('error'):
            card.append(label(item['error'], 'problem', wrap=True))
            return card
        line = f"{esc(item.get('branch'))} at <span font_family='monospace'>{esc(item.get('head'))}</span>"
        if item.get('based_on'):
            line += f" · {item.get('ahead')} patches on {esc(item['based_on'])}"
        else:
            line += f" · {item.get('ahead')} own commits"
        line += f" · upstream {esc(item.get('upstream'))}"
        if item.get('behind'):
            line += f" has <b>{item['behind']}</b> new"
        card.append(label(line, 'small', markup=True, wrap=True))
        if item.get('fetch_error'):
            card.append(label(f"fetch failed: {item['fetch_error']}", 'problem', wrap=True))
        log = item.get('upstream_log') or []
        if log:
            lines = box(spacing=1, margin_start=6)
            for entry in log[:4]:
                rev, _, subject = entry.partition(' ')
                row = box(False, 6)
                row.append(label(rev[:8], 'hash'))
                row.append(label(subject, 'commit', ellipsize=True, hexpand=True))
                lines.append(row)
            if item.get('behind', 0) > 4:
                lines.append(label(f"and {item['behind'] - 4} more", ['muted', 'small']))
            card.append(lines)
        if item.get('conflicts'):
            expander = Gtk.Expander(label=f"Conflicting files ({item.get('conflict_count')})", expanded=EXPAND)
            rows = box(spacing=0, margin_start=12)
            for f in item['conflicts'][:25]:
                rows.append(label(f, ['mono', 'muted'], ellipsize=True))
            expander.set_child(rows)
            card.append(expander)
        row = box(False, 6)
        name = {'name': item['name']}
        tree = item.get('worktree') or {}
        dirty_hint = (f"{tree.get('path')} has uncommitted changes" if tree and not tree.get('clean') else None)
        if status == 'fast-forward':
            row.append(self.action_button('Fast-forward', host, 'fork-apply', dict(name, how='fast-forward'),
                                          suggested=True, sensitive=not dirty_hint, hint=dirty_hint))
        elif status == 'clean-merge':
            row.append(self.action_button('Merge', host, 'fork-apply', dict(name, how='merge'), confirm='Merge it?',
                                          suggested=True, sensitive=not dirty_hint,
                                          hint=dirty_hint or f"Merge {item.get('upstream')} into {item.get('branch')} "
                                                             '(local only; push is separate)'))
        elif status == 'clean-rebase':
            row.append(self.action_button('Rebase', host, 'fork-apply', dict(name, how='rebase'), confirm='Rebase it?',
                                          suggested=True, sensitive=not dirty_hint,
                                          hint=dirty_hint or f"Replay the {item.get('ahead')} patches onto "
                                                             f"{item.get('upstream')} (local only)"))
        if status in ('conflicts', 'clean-merge', 'clean-rebase'):
            row.append(self.action_button('Ask agent', host, 'fork-agent', name, suggested=status == 'conflicts',
                                          hint='Start a T3 thread on ' + host + ' that ports the update in a new '
                                               'worktree and branch (older t3: the brief is copied instead)'))
        if item.get('push') and item.get('unpushed'):
            row.append(self.action_button(f"Push {item['unpushed']}", host, 'fork-push', name,
                                          confirm=f"Push to {item['push']}?",
                                          hint=f"git push {item['push']} {item.get('branch')}"
                                               + (' --force-with-lease' if item.get('strategy') == 'rebase' else '')))
        if row.get_first_child():
            card.append(row)
        return card


def main(argv):
    if argv[:1] != ['serve']:
        print('usage: update-manager panel serve [--open [PAGE]]', file=sys.stderr)
        return 2
    os.environ['LD_PRELOAD'] = os.environ.pop('UPDATE_MANAGER_PRELOADED', os.environ.get('LD_PRELOAD', ''))
    if not os.environ['LD_PRELOAD']:
        del os.environ['LD_PRELOAD']
    app = Panel()
    if argv[1:2] == ['--open']:
        page = argv[2] if len(argv) > 2 else None

        def opened():
            if page:
                app.activate_action('show', GLib.Variant('s', page))
            else:
                app.activate_action('toggle', None)
            return False
        app.connect('startup', lambda *_: GLib.idle_add(opened))
    return app.run([])

