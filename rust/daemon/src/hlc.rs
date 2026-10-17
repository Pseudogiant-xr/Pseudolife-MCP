//! The service hybrid logical clock (`memory/hlc.py` `HybridLogicalClock`),
//! the ordering authority for canonical writes: `(physical ms, logical)`,
//! monotonic whatever the wall clock does. `Ready::tick_hlc` seeds it from
//! the stored high-water mark as `MemoryService._reseed_hlc` does.
//!
//! The coordination board keeps its own clock (`board/hlc.rs`); Python
//! shares one object between the two (declared in `divergences.md`).

/// `_wall_ms`: `int(time.time() * 1000)`.
fn wall_ms() -> i64 {
    (crate::episodes::now() * 1000.0) as i64
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Clock {
    phys: i64,
    logical: i64,
}

impl Clock {
    /// `tick`: the next stamp at wall time `now_ms`.
    pub fn tick_at(&mut self, now_ms: i64) -> (i64, i64) {
        if now_ms > self.phys {
            self.phys = now_ms;
            self.logical = 0;
        } else {
            self.logical += 1;
        }
        (self.phys, self.logical)
    }

    /// `tick` on the wall clock.
    pub fn tick(&mut self) -> (i64, i64) {
        self.tick_at(wall_ms())
    }

    /// `observe`: advance past a stamp seen on read.
    pub fn observe(&mut self, phys: i64, logical: i64) {
        if phys > self.phys {
            self.phys = phys;
            self.logical = logical;
        } else if phys == self.phys {
            self.logical = self.logical.max(logical);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tick_follows_the_wall_clock_and_never_goes_back() {
        let mut c = Clock::default();
        assert_eq!(c.tick_at(1000), (1000, 0));
        assert_eq!(c.tick_at(1000), (1000, 1));
        // A wall-clock step back bumps the counter instead.
        assert_eq!(c.tick_at(900), (1000, 2));
        assert_eq!(c.tick_at(1001), (1001, 0));
    }

    #[test]
    fn observe_moves_past_a_stored_stamp() {
        let mut c = Clock::default();
        c.observe(5000, 3);
        // The wall clock is behind the stored stamp: the next tick outranks it.
        assert_eq!(c.tick_at(4000), (5000, 4));
        // An older or equal stamp never moves the clock back.
        c.observe(4500, 9);
        c.observe(5000, 2);
        assert_eq!(c.tick_at(4000), (5000, 5));
        c.observe(5000, 10);
        assert_eq!(c.tick_at(4000), (5000, 11));
        assert!(Clock::default().tick() > (1_600_000_000_000, -1));
    }
}
