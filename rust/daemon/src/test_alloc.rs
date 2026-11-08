//! Test-only allocation meter. The daemon's test binary forwards every
//! allocation to the system allocator and counts live bytes per thread, so
//! a test can bound what one call holds at its peak (`peak_during`).
//!
//! Exception to the crate's safe-code rule: a `GlobalAlloc` is an unsafe
//! trait. Compiled only under `cfg(test)`; the daemon never ships it.
#![allow(unsafe_code)]

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

thread_local! {
    static LIVE: Cell<isize> = const { Cell::new(0) };
    static PEAK: Cell<isize> = const { Cell::new(0) };
}

/// Forwards to [`System`] and counts the bytes each thread holds.
pub(crate) struct Meter;

fn count(delta: isize) {
    // `try_with`: allocations during thread teardown are forwarded, not counted.
    let _ = LIVE.try_with(|live| {
        let now = live.get() + delta;
        live.set(now);
        let _ = PEAK.try_with(|peak| {
            if now > peak.get() {
                peak.set(now);
            }
        });
    });
}

fn signed(n: usize) -> isize {
    isize::try_from(n).unwrap_or(isize::MAX)
}

// SAFETY: every method forwards its arguments unchanged to `System`, which
// upholds the `GlobalAlloc` contract. The counters are const-initialized
// thread-local `Cell`s with no destructor, so updating them never allocates
// or re-enters the allocator.
unsafe impl GlobalAlloc for Meter {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // SAFETY: the caller's contract for `layout` is passed through.
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            count(signed(layout.size()));
        }
        ptr
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        // SAFETY: as `alloc`.
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if !ptr.is_null() {
            count(signed(layout.size()));
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: `ptr` came from this allocator, which got it from `System`.
        unsafe { System.dealloc(ptr, layout) };
        count(-signed(layout.size()));
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        // SAFETY: as `dealloc`, with the caller's `new_size` contract.
        let out = unsafe { System.realloc(ptr, layout, new_size) };
        if !out.is_null() {
            count(signed(new_size) - signed(layout.size()));
        }
        out
    }
}

#[global_allocator]
static METER: Meter = Meter;

/// Runs `f` on this thread and returns its result with the most bytes it
/// held live at once above what the thread held when it started.
pub(crate) fn peak_during<T>(f: impl FnOnce() -> T) -> (T, usize) {
    let base = LIVE.with(Cell::get);
    PEAK.with(|peak| peak.set(base));
    let out = f();
    let peak = PEAK.with(Cell::get);
    (out, usize::try_from(peak - base).unwrap_or(0))
}
