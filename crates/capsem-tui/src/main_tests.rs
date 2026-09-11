use std::sync::mpsc;
use std::time::{Duration, Instant};

use super::{
    apply_refresh_event, handle_input_event_batch, handle_terminal_event, mouse_input_for,
    terminal_event_closes_connection, ConnectedTerminal, RefreshBridge, RefreshEvent,
};
use capsem_tui::app::App;
use capsem_tui::fixture::offline_state;
use capsem_tui::model::ServiceStatus;
use capsem_tui::terminal::{TerminalEvent, TerminalSurface};
use capsem_tui::ui::terminal_area;
use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;

#[test]
fn terminal_failure_status_clears_connected_session() {
    let connected = ConnectedTerminal {
        session_id: "vm-1".to_string(),
        cols: 80,
        rows: 23,
    };
    let event = TerminalEvent::Status {
        session_id: "vm-1".to_string(),
        status: "connect failed: refused".to_string(),
    };

    assert!(terminal_event_closes_connection(&event, Some(&connected)));
}

#[test]
fn terminal_connected_status_keeps_connected_session() {
    let connected = ConnectedTerminal {
        session_id: "vm-1".to_string(),
        cols: 80,
        rows: 23,
    };
    let event = TerminalEvent::Status {
        session_id: "vm-1".to_string(),
        status: "connected".to_string(),
    };

    assert!(!terminal_event_closes_connection(&event, Some(&connected)));
}

#[test]
fn refresh_bridge_keeps_slow_gateway_load_off_input_thread() {
    let (started_tx, started_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let bridge = RefreshBridge::spawn_with_loader(move || {
        started_tx.send(()).expect("signal refresh start");
        release_rx.recv().expect("wait for test release");
        Ok(offline_state())
    });

    let started = Instant::now();
    bridge.request();
    assert!(
        started.elapsed() < Duration::from_millis(20),
        "requesting a refresh must not block the TUI input/render thread"
    );
    started_rx
        .recv_timeout(Duration::from_millis(250))
        .expect("refresh worker should start in the background");

    bridge.request();
    assert!(
        started_rx.recv_timeout(Duration::from_millis(50)).is_err(),
        "a slow refresh must not let periodic ticks queue duplicate gateway loads"
    );
    assert!(bridge.drain_events().is_empty());

    release_tx.send(()).expect("release refresh worker");
    let events = wait_for_refresh_events(&bridge);
    assert_eq!(events.len(), 1);
    assert!(matches!(events.first(), Some(RefreshEvent::Loaded(_))));
}

#[test]
fn failed_refresh_event_marks_service_offline_without_blocking() {
    let mut state = offline_state();
    state.service.reconnect_attempt = None;
    let mut app = App::new(state);
    let changed = apply_refresh_event(&mut app, RefreshEvent::Failed("timeout".to_string()));

    assert!(changed);
    assert_eq!(app.state().service.status, ServiceStatus::Offline);
    assert_eq!(app.state().service.reconnect_attempt, Some(1));
}

#[test]
fn input_event_batch_drains_ready_events_before_redraw() {
    let (queued_tx, queued_rx) = mpsc::channel();
    for ch in ['b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'] {
        queued_tx.send(Ok(key_event(ch))).expect("queue ready terminal input");
    }
    drop(queued_tx);

    let mut handled = Vec::new();
    let should_exit = handle_input_event_batch(Ok(key_event('a')), &queued_rx, |event| {
        handled.push(key_char(event));
        Ok(false)
    })
    .expect("drain ready input batch");

    assert!(!should_exit);
    assert_eq!(handled, vec!['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i']);
    assert!(
        queued_rx.try_recv().is_err(),
        "all ready input must be handled before the TUI redraws"
    );
}

#[test]
fn input_event_batch_is_bounded_so_rendering_cannot_starve() {
    let (queued_tx, queued_rx) = mpsc::channel();
    for _ in 1..=(super::MAX_INPUT_EVENTS_PER_TICK + 10) {
        queued_tx.send(Ok(key_event('x'))).expect("queue terminal input flood");
    }
    drop(queued_tx);

    let mut handled = 0usize;
    let should_exit = handle_input_event_batch(Ok(key_event('x')), &queued_rx, |_event| {
        handled += 1;
        Ok(false)
    })
    .expect("handle bounded input batch");

    assert!(!should_exit);
    assert_eq!(handled, super::MAX_INPUT_EVENTS_PER_TICK);
    assert!(
        queued_rx.try_recv().is_ok(),
        "input floods must yield back to the render/gateway loop after one bounded batch"
    );
}

#[test]
fn input_event_batch_stops_on_exit_without_draining_extra_events() {
    let (queued_tx, queued_rx) = mpsc::channel();
    queued_tx.send(Ok(key_event('b'))).expect("queue exit event");
    queued_tx.send(Ok(key_event('c'))).expect("queue event after exit");

    let mut handled = Vec::new();
    let should_exit = handle_input_event_batch(Ok(key_event('a')), &queued_rx, |event| {
        let ch = key_char(event);
        handled.push(ch);
        Ok(ch == 'b')
    })
    .expect("stop ready input batch");

    assert!(should_exit);
    assert_eq!(handled, vec!['a', 'b']);
    assert!(
        queued_rx.try_recv().is_ok(),
        "events after an exit action must remain untouched"
    );
}

fn wait_for_refresh_events(bridge: &RefreshBridge) -> Vec<RefreshEvent> {
    let deadline = Instant::now() + Duration::from_millis(500);
    loop {
        let events = bridge.drain_events();
        if !events.is_empty() {
            return events;
        }
        assert!(Instant::now() < deadline, "timed out waiting for refresh event");
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn key_event(ch: char) -> Event {
    Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE))
}

fn key_char(event: Event) -> char {
    let Event::Key(key) = event else {
        panic!("expected key event");
    };
    let KeyCode::Char(ch) = key.code else {
        panic!("expected char key");
    };
    ch
}

fn app_with_session(session_id: &str) -> App {
    let mut state = capsem_tui::fixture::fixture_state();
    if let Some(first) = state.sessions.first_mut() {
        first.id = session_id.to_string();
    }
    state.active_session_id = session_id.to_string();
    App::new(state)
}

#[test]
fn mouse_input_for_returns_none_when_guest_has_no_mouse_tracking() {
    let app = app_with_session("vm-1");
    let surface = TerminalSurface::new();
    let area = Rect::new(0, 0, 80, 23);
    let mouse = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 5,
        modifiers: KeyModifiers::NONE,
    };

    assert_eq!(mouse_input_for(&app, &surface, area, mouse), None);
}

#[test]
fn mouse_input_for_returns_none_outside_terminal_bounds() {
    let app = app_with_session("vm-1");
    let mut surface = TerminalSurface::new();
    surface.apply(TerminalEvent::Output {
        session_id: "vm-1".into(),
        bytes: b"\x1b[?1000h\x1b[?1006h".to_vec(),
    });

    let area = Rect::new(0, 0, 80, 23);

    // Mouse on status bar (row == 23)
    let click_status_bar = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 23,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(mouse_input_for(&app, &surface, area, click_status_bar), None);

    // Mouse out of bounds horizontally (column == 80)
    let click_out_of_width = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 80,
        row: 5,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(mouse_input_for(&app, &surface, area, click_out_of_width), None);

    // Mouse before origin with offset area
    let offset_area = Rect::new(5, 2, 80, 23);
    let click_before_x = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 4,
        row: 5,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(mouse_input_for(&app, &surface, offset_area, click_before_x), None);

    let click_before_y = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 1,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(mouse_input_for(&app, &surface, offset_area, click_before_y), None);
}

#[test]
fn mouse_input_for_returns_none_when_overlay_is_active() {
    let mut app = app_with_session("vm-1");
    let mut surface = TerminalSurface::new();
    surface.apply(TerminalEvent::Output {
        session_id: "vm-1".into(),
        bytes: b"\x1b[?1000h\x1b[?1006h".to_vec(),
    });

    // Open Help overlay via Alt+?
    app.handle_key(KeyEvent::new(KeyCode::Char('?'), KeyModifiers::ALT));
    assert_ne!(app.overlay(), capsem_tui::app::AppOverlay::None);

    let area = Rect::new(0, 0, 80, 23);
    let mouse = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 5,
        modifiers: KeyModifiers::NONE,
    };

    assert_eq!(mouse_input_for(&app, &surface, area, mouse), None);
}

#[test]
fn mouse_input_for_returns_none_when_control_progress_is_active() {
    let mut app = app_with_session("vm-1");
    let mut surface = TerminalSurface::new();
    surface.apply(TerminalEvent::Output {
        session_id: "vm-1".into(),
        bytes: b"\x1b[?1000h\x1b[?1006h".to_vec(),
    });

    let area = Rect::new(0, 0, 80, 23);
    let mouse = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 5,
        modifiers: KeyModifiers::NONE,
    };

    app.set_control_progress("restarting vm");
    assert_eq!(mouse_input_for(&app, &surface, area, mouse), None);

    app.clear_control_progress();
    assert_eq!(
        mouse_input_for(&app, &surface, area, mouse),
        Some(b"\x1b[<0;11;6M".to_vec())
    );
}

#[test]
fn mouse_input_for_translates_coordinates_and_returns_sgr_bytes() {
    let app = app_with_session("vm-1");
    let mut surface = TerminalSurface::new();
    surface.apply(TerminalEvent::Output {
        session_id: "vm-1".into(),
        bytes: b"\x1b[?1000h\x1b[?1006h".to_vec(),
    });

    // Offset area with origin at (5, 2)
    let offset_area = Rect::new(5, 2, 80, 23);

    // Click at screen (15, 7) translates to relative surface (10, 5) -> 1-based (11, 6)
    let click = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 15,
        row: 7,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(
        mouse_input_for(&app, &surface, offset_area, click),
        Some(b"\x1b[<0;11;6M".to_vec())
    );

    // Release at screen (15, 7) translates to SGR release 'm'
    let release = MouseEvent {
        kind: MouseEventKind::Up(MouseButton::Left),
        column: 15,
        row: 7,
        modifiers: KeyModifiers::NONE,
    };
    assert_eq!(
        mouse_input_for(&app, &surface, offset_area, release),
        Some(b"\x1b[<0;11;6m".to_vec())
    );
}

#[test]
fn mouse_input_for_returns_default_x10_bytes_when_guest_requests_default_encoding() {
    let app = app_with_session("vm-1");
    let mut surface = TerminalSurface::new();
    // Guest requests mouse tracking without ?1006h SGR encoding
    surface.apply(TerminalEvent::Output {
        session_id: "vm-1".into(),
        bytes: b"\x1b[?1000h".to_vec(),
    });

    let area = Rect::new(0, 0, 80, 23);
    let click = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 5,
        modifiers: KeyModifiers::NONE,
    };

    // 0 + 32 = 32 = ' ', col 11+32=43='+', row 6+32=38='&'
    assert_eq!(
        mouse_input_for(&app, &surface, area, click),
        Some(b"\x1b[M +&".to_vec())
    );
}

#[test]
fn handle_terminal_event_dispatches_mouse_and_gates_on_connected_session() {
    let mut app = app_with_session("vm-1");
    let mut surface = TerminalSurface::new();
    surface.apply(TerminalEvent::Output {
        session_id: "vm-1".into(),
        bytes: b"\x1b[?1000h\x1b[?1006h".to_vec(),
    });

    let (bridge, mut command_rx) = capsem_tui::terminal::TerminalBridge::mock();
    let mut term_area = Rect::new(0, 0, 80, 23);
    let mouse = Event::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 5,
        modifiers: KeyModifiers::NONE,
    });

    // When connected_session_id is mismatched (e.g. during a session switch):
    // bridge must not receive forwarded bytes.
    let exit = handle_terminal_event(
        mouse.clone(),
        &mut app,
        &surface,
        &mut term_area,
        Some("other-session"),
        Some(&bridge),
        None,
    )
    .expect("handle mismatched mouse event");
    assert!(!exit);
    assert!(command_rx.try_recv().is_err());

    // When connected_session_id matches active_id:
    // bridge receives the encoded SGR input bytes.
    let exit = handle_terminal_event(
        mouse,
        &mut app,
        &surface,
        &mut term_area,
        Some("vm-1"),
        Some(&bridge),
        None,
    )
    .expect("handle mouse event");
    assert!(!exit);
    assert_eq!(
        command_rx.try_recv().unwrap(),
        capsem_tui::terminal::TerminalCommand::Input(b"\x1b[<0;11;6M".to_vec())
    );
}

#[test]
fn handle_terminal_event_updates_geometry_on_resize_and_notifies_bridge() {
    let mut app = app_with_session("vm-1");
    let surface = TerminalSurface::new();
    let (bridge, mut command_rx) = capsem_tui::terminal::TerminalBridge::mock();
    let mut term_area = Rect::new(0, 0, 80, 24);

    let resize = Event::Resize(120, 40);
    let exit = handle_terminal_event(
        resize,
        &mut app,
        &surface,
        &mut term_area,
        Some("vm-1"),
        Some(&bridge),
        None,
    )
    .expect("handle resize event");
    assert!(!exit);

    let expected = terminal_area(Rect::new(0, 0, 120, 40));
    assert_eq!(term_area, expected);
    assert_eq!(term_area.width, 120);
    assert_eq!(term_area.height, 39);

    assert_eq!(
        command_rx.try_recv().unwrap(),
        capsem_tui::terminal::TerminalCommand::Resize { cols: 120, rows: 39 }
    );
}
