//! numpy's float32 `np.dot` and `np.linalg.norm`: OpenBLAS `cblas_sdot`, in
//! the summation order of the kernel numpy's bundled OpenBLAS picks for the
//! host CPU.
//!
//! numpy 2.x wheels ship scipy-openblas, a DYNAMIC_ARCH build (0.3.31 on the
//! hosts this port is checked on) whose `DYNAMIC_LIST` holds five x86_64
//! cores: Prescott, Nehalem, Sandybridge, Haswell and SkylakeX. At load,
//! `driver/others/dynamic.c` (`gotoblas_dynamic_init`, `get_coretype`)
//! maps the CPU to a core type and every core type outside the list
//! aliases to one inside it. Each of the five computes `sdot` in its own
//! order:
//!
//! * Nehalem, Sandybridge, Haswell, SkylakeX: `kernel/x86_64/sdot.c` runs
//!   the core's `sdot_kernel_16` microkernel over the leading `n & -32`
//!   elements, sums the remainder's float32 products in a double, adds the
//!   kernel's float32 sum last and rounds the double to float32;
//!   * `sdot_microk_nehalem-2.c`: four 4-lane SSE accumulators, 16 elements
//!     a step, multiply then add;
//!   * `sdot_microk_sandy-2.c`: four 8-lane AVX accumulators, 32 elements a
//!     step, multiply then add;
//!   * `sdot_microk_haswell-2.c`: as Sandybridge with fused multiply-adds
//!     (Zen uses this kernel too);
//!   * `sdot_microk_skylakex-2.c`: four 16-lane AVX-512 accumulators over
//!     64-element steps, folded into four 8-lane ones for the last 32-step,
//!     fused multiply-adds (Cooperlake and Sapphire Rapids use it too);
//! * Prescott: `kernel/generic/dot.c` with the SSE3 universal intrinsics,
//!   four 4-lane accumulators over 16-element steps, then single 4-lane
//!   steps, then a float32 scalar tail, multiply then add throughout.
//!
//! [`detect`] mirrors `get_coretype` and the CPU feature tests it calls
//! (`support_avx`, `support_avx2`, `support_avx512` — which checks only
//! AVX512VL and the OS's opmask/ZMM state, not F/CD/BW/DQ —
//! `support_avx512_bf16`, `support_amx_bf16`), its fallback when the model
//! is unknown, and the build's aliasing. `OPENBLAS_CORETYPE` is not read:
//! it overrides the Python process's choice, which this process cannot see.
//!
//! Every kernel is plain Rust arithmetic, so its result never depends on
//! the instructions this process may use; the fused kernels only run under
//! `avx2,fma` code generation when the CPU has both, for speed.

use std::sync::LazyLock;

/// One distinct `sdot` summation order in numpy's OpenBLAS build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Kernel {
    /// `kernel/generic/dot.c` (SSE3 universal intrinsics).
    Prescott,
    /// `sdot.c` + `sdot_microk_nehalem-2.c`.
    Nehalem,
    /// `sdot.c` + `sdot_microk_sandy-2.c`.
    Sandybridge,
    /// `sdot.c` + `sdot_microk_haswell-2.c`.
    Haswell,
    /// `sdot.c` + `sdot_microk_skylakex-2.c`.
    SkylakeX,
}

#[cfg(test)]
impl Kernel {
    pub(crate) const ALL: [Kernel; 5] = [
        Kernel::Prescott,
        Kernel::Nehalem,
        Kernel::Sandybridge,
        Kernel::Haswell,
        Kernel::SkylakeX,
    ];

    /// The golden key (`harness/gen_w2e_deep_dream_goldens.py`'s `KERNELS`).
    pub(crate) fn name(self) -> &'static str {
        match self {
            Kernel::Prescott => "prescott",
            Kernel::Nehalem => "nehalem",
            Kernel::Sandybridge => "sandybridge",
            Kernel::Haswell => "haswell",
            Kernel::SkylakeX => "skylakex",
        }
    }
}

/// The host's kernel, detected once.
static HOST: LazyLock<Kernel> = LazyLock::new(detect);

#[cfg(test)]
thread_local! {
    /// A test's forced kernel (per thread, so parallel tests do not mix).
    static FORCED: std::cell::Cell<Option<Kernel>> = const { std::cell::Cell::new(None) };
}

/// Run `f` with [`sdot`] forced to `k` on this thread.
#[cfg(test)]
pub(crate) fn with_kernel<T>(k: Kernel, f: impl FnOnce() -> T) -> T {
    let prev = FORCED.with(|c| c.replace(Some(k)));
    let out = f();
    FORCED.with(|c| c.set(prev));
    out
}

/// The kernel [`sdot`] uses.
pub(crate) fn active() -> Kernel {
    #[cfg(test)]
    if let Some(k) = FORCED.with(std::cell::Cell::get) {
        return k;
    }
    *HOST
}

/// `cblas_sdot(n, x, 1, y, 1)` with `n` the shorter length, in the host
/// kernel's order.
pub(crate) fn sdot(x: &[f32], y: &[f32]) -> f32 {
    sdot_with(active(), x, y)
}

/// `cblas_sdot` in `k`'s order.
pub(crate) fn sdot_with(k: Kernel, x: &[f32], y: &[f32]) -> f32 {
    let n = x.len().min(y.len());
    let (x, y) = (&x[..n], &y[..n]);
    match k {
        Kernel::Prescott => generic_dot(x, y),
        _ => sdot_c(k, x, y),
    }
}

// ── the kernels ──────────────────────────────────────────────────────────

/// `kernel/x86_64/sdot.c`, unit strides: the microkernel over `n & -32`,
/// the remainder's float32 products summed in a double, the kernel's sum
/// added last, the double returned as float32.
fn sdot_c(k: Kernel, x: &[f32], y: &[f32]) -> f32 {
    let n = x.len();
    if n == 0 {
        return 0.0;
    }
    let n1 = n & !31;
    let mydot = if n1 > 0 {
        microkernel(k, &x[..n1], &y[..n1])
    } else {
        0.0
    };
    let mut dot = 0f64;
    for i in n1..n {
        dot += f64::from(y[i] * x[i]);
    }
    dot += f64::from(mydot);
    dot as f32
}

/// `sdot_kernel_16` for `n` a positive multiple of 32.
fn microkernel(k: Kernel, x: &[f32], y: &[f32]) -> f32 {
    match k {
        Kernel::Nehalem => nehalem(x, y),
        Kernel::Sandybridge => sandy_haswell::<false>(x, y),
        Kernel::Haswell => {
            #[cfg(target_arch = "x86_64")]
            if *AVX2_FMA {
                // SAFETY: `AVX2_FMA` saw the CPU report both AVX2 and FMA,
                // every feature `haswell_avx2_fma` enables.
                return unsafe { haswell_avx2_fma(x, y) };
            }
            sandy_haswell::<true>(x, y)
        }
        Kernel::SkylakeX => {
            #[cfg(target_arch = "x86_64")]
            if *AVX2_FMA {
                // SAFETY: as above, for `skylakex_avx2_fma`.
                return unsafe { skylakex_avx2_fma(x, y) };
            }
            skylakex(x, y)
        }
        Kernel::Prescott => unreachable!("the generic dot has no microkernel"),
    }
}

/// Whether the CPU has every feature the `avx2,fma` kernels enable.
#[cfg(target_arch = "x86_64")]
static AVX2_FMA: LazyLock<bool> = LazyLock::new(|| {
    std::arch::is_x86_feature_detected!("avx2") && std::arch::is_x86_feature_detected!("fma")
});

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn haswell_avx2_fma(x: &[f32], y: &[f32]) -> f32 {
    sandy_haswell::<true>(x, y)
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn skylakex_avx2_fma(x: &[f32], y: &[f32]) -> f32 {
    skylakex(x, y)
}

/// `a + x * y`, fused or not.
#[inline(always)]
fn madd<const FUSED: bool>(x: f32, y: f32, a: f32) -> f32 {
    if FUSED { x.mul_add(y, a) } else { a + x * y }
}

/// `(v0 + v1) + (v2 + v3)`: two `haddps` of a vector with itself.
#[inline(always)]
fn hadd2(v: [f32; 4]) -> f32 {
    (v[0] + v[1]) + (v[2] + v[3])
}

/// `sdot_microk_nehalem-2.c`: `xmm4..7 += x * y` (`mulps`, `addps`) over
/// 16-element steps, then `(a0 + a1) + (a2 + a3)` and two `haddps`.
#[inline(always)]
fn nehalem(x: &[f32], y: &[f32]) -> f32 {
    let mut acc = [[0f32; 4]; 4];
    for (xc, yc) in x.chunks_exact(16).zip(y.chunks_exact(16)) {
        for (k, a4) in acc.iter_mut().enumerate() {
            for (l, a) in a4.iter_mut().enumerate() {
                *a = madd::<false>(xc[4 * k + l], yc[4 * k + l], *a);
            }
        }
    }
    let mut v = [0f32; 4];
    for (l, s) in v.iter_mut().enumerate() {
        *s = (acc[0][l] + acc[1][l]) + (acc[2][l] + acc[3][l]);
    }
    hadd2(v)
}

/// `sdot_microk_sandy-2.c` (`vmulps`, `vaddps`) and
/// `sdot_microk_haswell-2.c` (`vfmadd231ps`): `ymm4..7` over 32-element
/// steps; each accumulator's high 128 bits added to its low, then
/// `(q0 + q1) + (q2 + q3)` and two `vhaddps`.
#[inline(always)]
fn sandy_haswell<const FUSED: bool>(x: &[f32], y: &[f32]) -> f32 {
    let mut acc = [[0f32; 8]; 4];
    for (xc, yc) in x.chunks_exact(32).zip(y.chunks_exact(32)) {
        for (k, a8) in acc.iter_mut().enumerate() {
            for (l, a) in a8.iter_mut().enumerate() {
                *a = madd::<FUSED>(xc[8 * k + l], yc[8 * k + l], *a);
            }
        }
    }
    let q = |k: usize, l: usize| acc[k][l] + acc[k][l + 4];
    let mut v = [0f32; 4];
    for (l, s) in v.iter_mut().enumerate() {
        *s = (q(0, l) + q(1, l)) + (q(2, l) + q(3, l));
    }
    hadd2(v)
}

/// `sdot_microk_skylakex-2.c` (built with `__AVX512CD__`): four 16-lane
/// accumulators over 64-element steps (`accum += x * y`, contracted to an
/// FMA), each folded low 8 + high 8 into an 8-lane one, a 32-element step
/// for the remainder, then `((a0 + a1) + a2) + a3`, low 128 + high 128 and
/// two `haddps`.
#[inline(always)]
fn skylakex(x: &[f32], y: &[f32]) -> f32 {
    let n = x.len();
    let n64 = n & !63;
    let mut wide = [[0f32; 16]; 4];
    for (xc, yc) in x[..n64].chunks_exact(64).zip(y[..n64].chunks_exact(64)) {
        for (k, a16) in wide.iter_mut().enumerate() {
            for (l, a) in a16.iter_mut().enumerate() {
                *a = madd::<true>(xc[16 * k + l], yc[16 * k + l], *a);
            }
        }
    }
    let mut acc = [[0f32; 8]; 4];
    for (a8, w) in acc.iter_mut().zip(&wide) {
        for (l, a) in a8.iter_mut().enumerate() {
            *a = w[l] + w[l + 8];
        }
    }
    for (xc, yc) in x[n64..].chunks_exact(32).zip(y[n64..].chunks_exact(32)) {
        for (k, a8) in acc.iter_mut().enumerate() {
            for (l, a) in a8.iter_mut().enumerate() {
                *a = madd::<true>(xc[8 * k + l], yc[8 * k + l], *a);
            }
        }
    }
    let mut s = [0f32; 8];
    for (l, v) in s.iter_mut().enumerate() {
        *v = ((acc[0][l] + acc[1][l]) + acc[2][l]) + acc[3][l];
    }
    hadd2([s[0] + s[4], s[1] + s[5], s[2] + s[6], s[3] + s[7]])
}

/// `kernel/generic/dot.c` compiled with SSE3 (`V_SIMD` 128, no FMA): four
/// 4-lane accumulators over 16-element steps, combined as
/// `(v0 + v1) + (v2 + v3)`, single 4-lane steps into that, `v_sum_f32`
/// (two `haddps`), then the remainder added one float32 product at a time.
fn generic_dot(x: &[f32], y: &[f32]) -> f32 {
    let n = x.len();
    if n == 0 {
        return 0.0;
    }
    let unroll4 = n & !15;
    let unroll = n & !3;
    let mut acc = [[0f32; 4]; 4];
    for (xc, yc) in x[..unroll4]
        .chunks_exact(16)
        .zip(y[..unroll4].chunks_exact(16))
    {
        for (k, a4) in acc.iter_mut().enumerate() {
            for (l, a) in a4.iter_mut().enumerate() {
                *a = madd::<false>(xc[4 * k + l], yc[4 * k + l], *a);
            }
        }
    }
    let mut v = [0f32; 4];
    for (l, s) in v.iter_mut().enumerate() {
        *s = (acc[0][l] + acc[1][l]) + (acc[2][l] + acc[3][l]);
    }
    for (xc, yc) in x[unroll4..unroll]
        .chunks_exact(4)
        .zip(y[unroll4..unroll].chunks_exact(4))
    {
        for (l, s) in v.iter_mut().enumerate() {
            *s = madd::<false>(xc[l], yc[l], *s);
        }
    }
    let mut dot = hadd2(v);
    for i in unroll..n {
        dot += y[i] * x[i];
    }
    dot
}

// ── kernel selection (`driver/others/dynamic.c`) ─────────────────────────

/// `get_vendor`'s answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Vendor {
    Intel,
    Amd,
    Centaur,
    Hygon,
    Zhaoxin,
    Unknown,
}

/// The core types `get_coretype` returns (`corename[]`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Core {
    Katmai,
    Coppermine,
    Northwood,
    Prescott,
    Banias,
    Atom,
    Core2,
    Penryn,
    Dunnington,
    Nehalem,
    Athlon,
    Opteron,
    OpteronSse3,
    Barcelona,
    Nano,
    Sandybridge,
    Bobcat,
    Bulldozer,
    Piledriver,
    Haswell,
    Steamroller,
    Excavator,
    Zen,
    SkylakeX,
    Cooperlake,
    SapphireRapids,
}

/// What `get_coretype` and its fallback read from CPUID and XCR0.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct CpuFacts {
    pub vendor: Vendor,
    /// CPUID.1:EAX.
    pub leaf1_eax: u32,
    /// CPUID.1:ECX.
    pub leaf1_ecx: u32,
    /// The AMD `family <= 0xe` Athlon test: extended leaf 0x80000001
    /// exists and reports 3DNow! and 3DNow!+.
    pub amd_3dnow: bool,
    pub avx: bool,
    pub avx2: bool,
    pub avx512: bool,
    pub avx512_bf16: bool,
    pub amx_bf16: bool,
}

impl CpuFacts {
    fn family(&self) -> u32 {
        (self.leaf1_eax >> 8) & 0x0f
    }
    fn exfamily(&self) -> u32 {
        (self.leaf1_eax >> 20) & 0xff
    }
    fn model(&self) -> u32 {
        (self.leaf1_eax >> 4) & 0x0f
    }
    fn exmodel(&self) -> u32 {
        (self.leaf1_eax >> 16) & 0x0f
    }
    fn stepping(&self) -> u32 {
        self.leaf1_eax & 0x0f
    }

    /// The common "AVX2 → Haswell, AVX → Sandybridge, else Nehalem" chain.
    fn hsw(&self) -> Core {
        if self.avx2 {
            Core::Haswell
        } else if self.avx {
            Core::Sandybridge
        } else {
            Core::Nehalem
        }
    }

    /// "AVX-512 → SkylakeX", then [`Self::hsw`].
    fn skx(&self) -> Core {
        if self.avx512 {
            Core::SkylakeX
        } else {
            self.hsw()
        }
    }

    /// "AVX512-BF16 → Cooperlake", then [`Self::skx`].
    fn cpx(&self) -> Core {
        if self.avx512_bf16 {
            Core::Cooperlake
        } else {
            self.skx()
        }
    }

    fn sandy_or_nehalem(&self) -> Core {
        if self.avx {
            Core::Sandybridge
        } else {
            Core::Nehalem
        }
    }

    fn amd_avx(&self, core: Core) -> Core {
        if self.avx { core } else { Core::Barcelona }
    }
}

/// `get_coretype`: `None` where OpenBLAS returns NULL.
pub(crate) fn get_coretype(c: &CpuFacts) -> Option<Core> {
    let (family, exfamily, model, exmodel) = (c.family(), c.exfamily(), c.model(), c.exmodel());
    if c.vendor == Vendor::Intel {
        match family {
            0x6 => {
                return match (exmodel, model) {
                    (0, 0..=7) => Some(Core::Katmai),
                    (0, 8 | 10 | 11) => Some(Core::Coppermine),
                    (0, 9 | 13 | 14) => Some(Core::Banias),
                    (0, 15) => Some(Core::Core2),
                    (1, 6) => Some(Core::Core2),
                    (1, 7) => Some(Core::Penryn),
                    (1, 13) => Some(Core::Dunnington),
                    (1, 10 | 11 | 14 | 15) => Some(Core::Nehalem),
                    (1, 12) => Some(Core::Atom),
                    (2, 5 | 12 | 14 | 15) => Some(Core::Nehalem),
                    (2, 10 | 13) => Some(c.sandy_or_nehalem()),
                    (3, 10 | 14) => Some(c.sandy_or_nehalem()),
                    (3, 12 | 15 | 13) => Some(c.hsw()),
                    (3, 7) => Some(Core::Atom),
                    (4, 5 | 6 | 7 | 15 | 14) => Some(c.hsw()),
                    (4, 12 | 13) => Some(Core::Nehalem),
                    (5, 6 | 14 | 7) => Some(c.hsw()),
                    (5, 5) => Some(c.cpx()),
                    (5, 12 | 15) => Some(Core::Nehalem),
                    (6, 6) => Some(c.hsw()),
                    (6, 10 | 12) => Some(c.cpx()),
                    (7, 10) => Some(Core::Nehalem),
                    (7, 13 | 14) => Some(c.skx()),
                    (8, 12 | 13) => Some(c.skx()),
                    (8, 14) => Some(c.hsw()),
                    (8, 15) => Some(if c.amx_bf16 {
                        Core::SapphireRapids
                    } else {
                        c.cpx()
                    }),
                    (9, 7 | 10) => Some(c.cpx()),
                    (9, 14) => Some(c.hsw()),
                    (10, 5 | 6) => Some(c.hsw()),
                    (10, 7) => Some(c.skx()),
                    _ => None,
                };
            }
            0xf => {
                return Some(if model <= 0x2 {
                    Core::Northwood
                } else {
                    Core::Prescott
                });
            }
            _ => {}
        }
    }
    if matches!(c.vendor, Vendor::Amd | Vendor::Hygon) {
        if family <= 0xe {
            return c.amd_3dnow.then_some(Core::Athlon);
        }
        if family == 0xf {
            return match exfamily {
                0 | 2 => Some(if c.leaf1_ecx & 1 != 0 {
                    Core::OpteronSse3
                } else {
                    Core::Opteron
                }),
                5 | 7 => Some(Core::Bobcat),
                6 => match (model, exmodel) {
                    (1, _) => Some(c.amd_avx(Core::Bulldozer)),
                    (2 | 3, _) => Some(c.amd_avx(Core::Piledriver)),
                    (5, _) => Some(c.amd_avx(Core::Excavator)),
                    (0 | 8, 1) => Some(c.amd_avx(Core::Piledriver)),
                    (0 | 8, 3) => Some(c.amd_avx(Core::Steamroller)),
                    (0 | 8, 6) => Some(c.amd_avx(Core::Excavator)),
                    _ => None,
                },
                8 | 9 => Some(c.amd_avx(Core::Zen)),
                10 => Some(if c.avx512_bf16 {
                    Core::Cooperlake
                } else if c.avx512 {
                    Core::SkylakeX
                } else {
                    c.amd_avx(Core::Zen)
                }),
                _ => None,
            };
        }
    }
    if c.vendor == Vendor::Centaur {
        match family {
            0x6 => {
                return Some(if model == 0xf && c.stepping() < 0xe {
                    Core::Nano
                } else {
                    Core::Nehalem
                });
            }
            0x7 => {
                return Some(match exmodel {
                    5 | 6 if c.avx2 => Core::Zen,
                    5 | 6 => Core::Dunnington,
                    _ => Core::Nehalem,
                });
            }
            f if f >= 0x8 => return Some(Core::Nehalem),
            _ => {}
        }
    }
    if c.vendor == Vendor::Zhaoxin {
        return Some(match (family, exmodel) {
            (0x7, 5) if c.avx2 => Core::Zen,
            (0x7, 5) => Core::Dunnington,
            _ => Core::Nehalem,
        });
    }
    None
}

/// `gotoblas_dynamic_init` without `OPENBLAS_CORETYPE` on x86_64:
/// `get_coretype`, its feature fallback when that is NULL, and the 64-bit
/// sanity check.
pub(crate) fn dynamic_core(c: &CpuFacts) -> Core {
    let core = get_coretype(c).unwrap_or(if c.avx512_bf16 {
        Core::Cooperlake
    } else if c.avx512 {
        Core::SkylakeX
    } else if c.avx2 {
        Core::Haswell
    } else if c.avx {
        Core::Sandybridge
    } else {
        Core::Prescott
    });
    match core {
        Core::Katmai | Core::Coppermine | Core::Northwood | Core::Banias | Core::Athlon => {
            Core::Prescott
        }
        other => other,
    }
}

/// The kernel a core type runs in scipy-openblas's build: the core types
/// outside its `DYNAMIC_LIST` alias as `dynamic.c`'s `DYN_*` chains define
/// (confirmed by forcing each name through `OPENBLAS_CORETYPE` and reading
/// the reported core).
pub(crate) fn kernel_for(core: Core) -> Kernel {
    match core {
        Core::Katmai
        | Core::Coppermine
        | Core::Northwood
        | Core::Prescott
        | Core::Banias
        | Core::Core2
        | Core::Penryn
        | Core::Dunnington
        | Core::Athlon
        | Core::Opteron
        | Core::OpteronSse3
        | Core::Nano => Kernel::Prescott,
        Core::Atom | Core::Nehalem | Core::Barcelona | Core::Bobcat => Kernel::Nehalem,
        Core::Sandybridge
        | Core::Bulldozer
        | Core::Piledriver
        | Core::Steamroller
        | Core::Excavator => Kernel::Sandybridge,
        Core::Haswell | Core::Zen => Kernel::Haswell,
        Core::SkylakeX | Core::Cooperlake | Core::SapphireRapids => Kernel::SkylakeX,
    }
}

/// The host's kernel. Off x86_64 numpy runs other OpenBLAS kernels this
/// port does not reproduce; SkylakeX stands in (declared).
pub(crate) fn detect() -> Kernel {
    #[cfg(target_arch = "x86_64")]
    {
        kernel_for(dynamic_core(&host_facts()))
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        Kernel::SkylakeX
    }
}

/// CPUID leaf `leaf`, subleaf `sub`.
#[cfg(target_arch = "x86_64")]
fn cpuid(leaf: u32, sub: u32) -> [u32; 4] {
    let r = std::arch::x86_64::__cpuid_count(leaf, sub);
    [r.eax, r.ebx, r.ecx, r.edx]
}

/// XCR0, the OS-enabled state components.
///
/// # Safety
/// The CPU must report OSXSAVE (CPUID.1:ECX bit 27).
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "xsave")]
unsafe fn xcr0() -> u64 {
    // SAFETY: the caller checked OSXSAVE, so XGETBV is enabled.
    unsafe { std::arch::x86_64::_xgetbv(0) }
}

/// Read the facts `get_coretype` uses, as OpenBLAS reads them (`cpuid` with
/// ECX zeroed; leaves read without checking the maximum leaf).
#[cfg(target_arch = "x86_64")]
pub(crate) fn host_facts() -> CpuFacts {
    let [max, ebx0, ecx0, edx0] = cpuid(0, 0);
    let mut name = [0u8; 12];
    name[..4].copy_from_slice(&ebx0.to_le_bytes());
    name[4..8].copy_from_slice(&edx0.to_le_bytes());
    name[8..].copy_from_slice(&ecx0.to_le_bytes());
    let vendor = match &name {
        b"GenuineIntel" => Vendor::Intel,
        b"AuthenticAMD" => Vendor::Amd,
        b"CentaurHauls" => Vendor::Centaur,
        b"  Shanghai  " => Vendor::Zhaoxin,
        b"HygonGenuine" => Vendor::Hygon,
        _ if max == 0 || max & 0x500 != 0 => Vendor::Intel,
        _ => Vendor::Unknown,
    };
    let [eax1, _, ecx1, _] = cpuid(1, 0);
    // `support_avx`: AVX, OSXSAVE and XSAVE, and XCR0 enabling SSE + AVX.
    let osxsave = ecx1 & (1 << 27) != 0;
    let xcr = if ecx1 & (1 << 28) != 0 && osxsave && ecx1 & (1 << 26) != 0 {
        // SAFETY: OSXSAVE was just checked.
        Some(unsafe { xcr0() })
    } else {
        None
    };
    let avx = xcr.is_some_and(|x| x & 6 == 6);
    let [_, ebx7, _, edx7] = cpuid(7, 0);
    let avx2 = avx && ebx7 & (1 << 5) != 0;
    // `support_avx512`: AVX512VL plus the opmask and both ZMM state
    // components in XCR0 (no F/CD/BW/DQ test).
    let avx512 = avx && ebx7 & (1 << 31) != 0 && xcr.is_some_and(|x| x & 0xe0 == 0xe0);
    let avx512_bf16 = avx512 && cpuid(7, 1)[0] & 32 == 32;
    let amx_bf16 = avx512
        && edx7 & 0x0100_0000 != 0
        && edx7 & 0x0040_0000 != 0
        && cpuid(0xd, 0)[0] & 0x0006_0000 == 0x0006_0000;
    let family = (eax1 >> 8) & 0x0f;
    let amd_3dnow = matches!(vendor, Vendor::Amd | Vendor::Hygon) && family <= 0xe && {
        let ext = cpuid(0x8000_0000, 0)[0];
        ext & 0xffff >= 1 && {
            let edx = cpuid(0x8000_0001, 0)[3];
            edx & (1 << 30) != 0 && edx & (1 << 31) != 0
        }
    };
    CpuFacts {
        vendor,
        leaf1_eax: eax1,
        leaf1_ecx: ecx1,
        amd_3dnow,
        avx,
        avx2,
        avx512,
        avx512_bf16,
        amx_bf16,
    }
}

#[cfg(test)]
#[path = "openblas_sdot_tests.rs"]
mod tests;
