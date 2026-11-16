//! Whether a console child of this process should be started without a
//! window of its own.
//!
//! At a terminal a child shares this process's console, so its window
//! never opens and anything it shows (a prompt ssh's `BatchMode` does not
//! cover) stays visible. With no console attached (a scheduled task, a GUI
//! parent), Windows would give each console child a new visible window;
//! `CREATE_NO_WINDOW` keeps it hidden.

/// `CREATE_NO_WINDOW`.
#[cfg_attr(not(windows), allow(dead_code))]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// The flag to add for a console child, given how many processes this
/// process's console has (`GetConsoleProcessList`, which answers 0 when no
/// console is attached).
#[cfg_attr(not(windows), allow(dead_code))]
pub(super) fn window_flag(console_processes: u32) -> u32 {
    if console_processes == 0 {
        CREATE_NO_WINDOW
    } else {
        0
    }
}

/// The flag to add for a console child of this process: `CREATE_NO_WINDOW`
/// only when this process has no console.
#[cfg(windows)]
#[allow(unsafe_code)]
pub(in crate::cli) fn child_window_flag() -> u32 {
    use windows_sys::Win32::System::Console::GetConsoleProcessList;
    let mut members = [0_u32; 1];
    // SAFETY: `members` is a writable buffer of the length passed; the call
    // writes at most that many process ids and returns the console's
    // process count (0 when no console is attached, or on failure).
    let count = unsafe { GetConsoleProcessList(members.as_mut_ptr(), members.len() as u32) };
    window_flag(count)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_child_hides_its_window_only_when_this_process_has_no_console() {
        assert_eq!(window_flag(0), CREATE_NO_WINDOW);
        assert_eq!(window_flag(1), 0);
        assert_eq!(window_flag(3), 0);
    }
}
