"""Native Linux application entry point and metadata-only local control."""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .ipc import AlreadyRunning, COMMANDS, Instance, send_command


def _run_gui(args, service, done, request_quit, bind_activate):
    try:
        from PyQt6.QtCore import QObject, QTimer, pyqtSignal
        from PyQt6.QtGui import QAction, QIcon
        from PyQt6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon
        from .gui import MainWindow
    except ImportError:
        if done.is_set():
            return 0, None
        raise ValueError('The native interface is unavailable') from None
    # IPC and signals already exist while Qt imports. Their request is never
    # replaced by a new callback or forgotten before app.exec().
    if done.is_set():
        return 0, None

    class Bridge(QObject):
        show_requested = pyqtSignal()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Local Voice')
    app.setDesktopFileName('local-voice')
    window = MainWindow(service)
    icon = QIcon.fromTheme('audio-input-microphone')
    if icon.isNull():
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
    app.setWindowIcon(icon)
    bridge = Bridge()

    def cancel_quit():
        done.clear()
        window.close_cancelled_by_user = False
    window.close_cancel_callback = cancel_quit

    def show():
        window.showNormal()
        window.raise_()
        window.activateWindow()

    polling_quit = False
    def poll_quit():
        nonlocal polling_quit
        if polling_quit:
            return
        polling_quit = True
        try:
            if getattr(window, 'close_cancelled_by_user', False):
                window.close_cancelled_by_user = False
                done.clear()
            elif not done.is_set():
                return
            elif window.request_close():
                app.quit()
            elif getattr(window, 'close_cancelled_by_user', False):
                window.close_cancelled_by_user = False
                done.clear()
        finally:
            polling_quit = False

    bridge.show_requested.connect(show)
    bind_activate(bridge.show_requested.emit)
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    # Qt registers a visible icon automatically if the panel starts later.
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip('Local Voice · offline')
    menu = QMenu()
    open_action = QAction('Open Local Voice', menu)
    open_action.triggered.connect(show)
    menu.addAction(open_action)
    exit_action = QAction('Quit', menu)
    exit_action.triggered.connect(request_quit)
    menu.addAction(exit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: show() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    app.setQuitOnLastWindowClosed(not tray_available)

    def can_hide():
        available = QSystemTrayIcon.isSystemTrayAvailable()
        app.setQuitOnLastWindowClosed(not available)
        return available

    window.close_to_tray = can_hide
    timer = QTimer()
    timer.timeout.connect(poll_quit)  # Also lets Python deliver POSIX signals.
    timer.start(200)
    if done.is_set():
        QTimer.singleShot(0, poll_quit)
    elif args.minimized:
        if not tray_available:
            window.show()
    else:
        window.show()
    result = app.exec()
    timer.stop()
    tray.hide()
    return result, window


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline Linux dictation and meetings')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--control', choices=sorted(COMMANDS))
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--minimized', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/share/local-voice')
    parser.add_argument('--config-dir', type=Path, default=Path.home()/'.config/local-voice')
    parser.add_argument('--runtime-dir', type=Path, default=Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/'+str(os.getuid())))/'local-voice')
    parser.add_argument('--app-dir', type=Path, default=Path.home()/'.local/opt/local-voice')
    parser.add_argument('--migrate-from', type=Path)
    args = parser.parse_args(argv)
    if args.control:
        result = send_command(args.runtime_dir, args.control)
        print(json.dumps(result))
        return 0 if result.get('ok') else 1
    try:
        with Instance(args.runtime_dir) as instance:
            from .settings import Settings
            from .store import Store
            settings = Settings(args.config_dir/'settings.json')
            store = Store(args.state_dir/'sessions.db')
            if args.migrate_from:
                from .migration import inspect_native
                from .native_import import import_native
                report = import_native(args.migrate_from, store)
                report['preferences'] = settings.import_native_preferences(inspect_native(args.migrate_from))
                print(json.dumps({'ok': True, 'import': report}))
                return 0
            from .runtime import SpeechRuntime
            from .service import Service
            from .summary import LocalSummarizer, SummarySpec
            model_lock = threading.Lock()
            speech = SpeechRuntime(args.app_dir, args.state_dir, settings, store, model_lock)
            summarizer = LocalSummarizer(SummarySpec(model_dir=args.state_dir/'models/qwen3-4b-instruct-2507-q4_k_m'),
                                         args.state_dir/'jobs', enabled=True)
            service = Service(store, settings, args.state_dir, lambda: speech,
                              summarizer=summarizer, model_lock=model_lock)
            done = threading.Event()
            activate = lambda: None
            request_quit = done.set

            def bind_activate(callback):
                nonlocal activate
                activate = callback

            def control(command):
                if command == 'status':
                    return service.public_status() | {'transport': speech.diagnostics()}
                if command in {'start', 'meeting'}:
                    if done.is_set():
                        return {'ok': False, 'error': 'The app is closing.'}
                    return {'ok': service.start('dictation' if command == 'start' else 'meeting')}
                if command == 'stop':
                    service.stop()
                elif command == 'activate':
                    activate()
                elif command == 'quit':
                    request_quit()
                return {'ok': True}

            instance.serve(control)
            signal.signal(signal.SIGTERM, lambda *_: request_quit())
            signal.signal(signal.SIGINT, lambda *_: request_quit())
            window = None
            try:
                if args.headless:
                    while not done.wait(.2):
                        pass
                    result = 0
                else:
                    result, window = _run_gui(args, service, done, request_quit, bind_activate)
            finally:
                clean = service.close()
                edits_accepted = window is None or window.shutdown_edits_accepted
                if clean and edits_accepted and window is not None:
                    try:
                        window.clear_recovery()
                    except (OSError, ValueError):
                        clean = False
                if not clean or not edits_accepted:
                    print('Shutdown is incomplete. Pending work and private edit recovery need attention.', file=sys.stderr)
            return result if clean and edits_accepted else 1
    except AlreadyRunning:
        result = send_command(args.runtime_dir, 'status' if args.minimized else 'activate')
        if args.headless:
            print(json.dumps(result))
        return 0 if result.get('ok') else 1
    except (OSError, ValueError) as error:
        # Do not print arbitrary exception values or private input.
        print('Local Voice could not open its private runtime. Check installation and permissions.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
