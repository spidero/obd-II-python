"""Show the OBDruid web UI in a GTK window, using the system's Python with GTK 3 and WebKit2GTK.

The standalone Linux build (AppImage) carries no window toolkit of its own - WebKit would make
it seven times bigger. When the system already has python3-gi and WebKit2GTK (common on GNOME
desktops), obdruid starts this script with the system Python to get a real window anyway.

Standalone on purpose: it must not import anything from obdruid, because it runs outside the build.

    python3 gtk_window.py --check                      exit code 0 when GTK and WebKit2 are available
    python3 gtk_window.py URL TITLE ICON DATA_DIR      show URL until the window is closed

Set OBDRUID_WINDOW_SMOKE=<seconds> to close the window that long after the page has loaded (tests).
"""

import os
import sys

NO_TOOLKIT = 3   # exit code: GTK or WebKit2 is missing


def _load():
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        for version in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", version)
                break
            except ValueError:
                continue
        else:
            return None
        from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2
    except (ImportError, ValueError):
        return None
    return Gdk, Gio, GLib, Gtk, WebKit2


def main(argv):
    toolkit = _load()
    if toolkit is None:
        return NO_TOOLKIT
    Gdk, Gio, GLib, Gtk, WebKit2 = toolkit
    if argv[1:] == ["--check"]:
        print("ok")
        return 0
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    url, title, icon, data_dir = argv[1:]
    origin = url.split("/", 3)
    origin = "/".join(origin[:3]) + "/" if len(origin) >= 3 else url
    GLib.set_prgname("OBDruid")            # name in the task bar and window class, not "gtk_window.py"
    GLib.set_application_name("OBDruid")
    Gdk.set_program_class("OBDruid")
    if not Gtk.init_check(None)[0]:
        return NO_TOOLKIT   # no display

    def own(uri):
        return uri.startswith(origin) or uri.startswith(("blob:", "data:", "about:"))

    def open_outside(uri):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            print(f"Could not open {uri}: {e.message}", file=sys.stderr)

    def on_policy(view, decision, kind):
        # links to other sites (GitHub, PayPal, e-mail) belong in the user's browser
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not own(uri):
                decision.ignore()
                open_outside(uri)
                return True
        return False

    def on_download(context, download):
        def destination(download, suggested):
            folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")
            path = os.path.join(folder, os.path.basename(suggested) or "obdruid-download")
            stem, ext = os.path.splitext(path)
            n = 1
            while os.path.exists(path):
                path, n = f"{stem} ({n}){ext}", n + 1
            download.set_destination(GLib.filename_to_uri(path, None))
            return True
        download.connect("decide-destination", destination)

    windows = []

    def new_window(view, related=None):
        window = Gtk.Window(title=title)
        window.set_default_size(1280, 820)
        if os.path.isfile(icon):
            try:
                window.set_icon_from_file(icon)
            except GLib.Error:
                pass
        view.connect("notify::title", lambda v, _: window.set_title(v.get_title() or title))
        view.connect("decide-policy", on_policy)
        view.connect("create", on_create)
        view.connect("close", lambda v: window.destroy())
        window.add(view)
        windows.append(window)
        window.connect("destroy", lambda w: windows.remove(w) or (windows or Gtk.main_quit()))
        return window

    def on_create(view, action):
        # window.open() of our own pages (the printable report): a second window of the app
        child = WebKit2.WebView.new_with_related_view(view)
        window = new_window(child)
        child.connect("ready-to-show", lambda v: window.show_all())
        return child

    manager = WebKit2.WebsiteDataManager(base_data_directory=os.path.join(data_dir, "data"),
                                         base_cache_directory=os.path.join(data_dir, "cache"))
    context = WebKit2.WebContext.new_with_website_data_manager(manager)
    context.connect("download-started", on_download)
    view = WebKit2.WebView.new_with_context(context)
    view.get_settings().set_javascript_can_open_windows_automatically(True)
    smoke = os.environ.get("OBDRUID_WINDOW_SMOKE")
    if smoke:
        def loaded(v, event):
            if event == WebKit2.LoadEvent.FINISHED:
                print(f"loaded: {v.get_uri()}", flush=True)
                GLib.timeout_add(int(float(smoke) * 1000), Gtk.main_quit)
        view.connect("load-changed", loaded)
    new_window(view).show_all()
    view.load_uri(url)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
