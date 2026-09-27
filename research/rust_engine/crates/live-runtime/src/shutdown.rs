#[cfg(windows)]
use std::sync::atomic::{AtomicBool, Ordering};

#[cfg(windows)]
static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

#[cfg(windows)]
unsafe extern "system" {
    fn SetConsoleCtrlHandler(
        handler: Option<unsafe extern "system" fn(u32) -> i32>,
        add: i32,
    ) -> i32;
}

#[cfg(windows)]
unsafe extern "system" fn console_control_handler(event: u32) -> i32 {
    if matches!(event, 0 | 1 | 2 | 5 | 6) {
        SHUTDOWN_REQUESTED.store(true, Ordering::SeqCst);
        1
    } else {
        0
    }
}

#[cfg(windows)]
pub fn install() -> Result<(), String> {
    let installed = unsafe { SetConsoleCtrlHandler(Some(console_control_handler), 1) };
    if installed == 0 {
        return Err("windows_console_shutdown_handler_install_failed".to_owned());
    }
    Ok(())
}

#[cfg(windows)]
pub fn requested() -> bool {
    SHUTDOWN_REQUESTED.load(Ordering::SeqCst)
}

#[cfg(not(windows))]
pub fn install() -> Result<(), String> {
    Ok(())
}

#[cfg(not(windows))]
pub fn requested() -> bool {
    false
}
