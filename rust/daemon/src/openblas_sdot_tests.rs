//! Each kernel against numpy run with `OPENBLAS_CORETYPE` forced to it
//! (`harness/gen_w2e_deep_dream_goldens.py`), whatever the host; the
//! dispatcher against `dynamic.c`'s table on synthetic CPUs.

use super::*;
use serde_json::Value;

const GOLDEN: &str = include_str!("../harness/goldens/w2e-deep-dream.json");

/// The generator's `sdot_input(seed, n)`.
fn sdot_input(seed: u64, n: u64) -> Vec<f32> {
    (0..n)
        .map(|j| {
            let h = (seed * 2654435761 + j * 40503 + 12345) % (1 << 32);
            let m = (h % (1 << 23)) as i64 - (1 << 22);
            let e = ((h >> 23) % 24) as u32;
            // 2^-(e + 10), exactly.
            m as f32 * f32::from_bits((127 - (e + 10)) << 23)
        })
        .collect()
}

fn bits(v: f32) -> String {
    hex::encode(v.to_le_bytes())
}

#[test]
fn every_kernel_matches_numpy_under_its_core_type() {
    let g: Value = serde_json::from_str(GOLDEN).unwrap();
    for k in Kernel::ALL {
        let cases = g["kernels"][k.name()]["sdot"].as_array().unwrap();
        assert!(!cases.is_empty(), "{k:?}");
        for case in cases {
            let n = case["n"].as_u64().unwrap();
            let (x, y) = (sdot_input(2 * n + 1, n), sdot_input(2 * n + 2, n));
            assert_eq!(bits(sdot_with(k, &x, &y)), case["xy"], "{k:?} n={n} x.y");
            assert_eq!(bits(sdot_with(k, &x, &x)), case["xx"], "{k:?} n={n} x.x");
            assert_eq!(
                bits(sdot_with(k, &y, &y).sqrt()),
                case["norm"],
                "{k:?} n={n} |y|"
            );
        }
    }
}

#[test]
fn the_kernels_really_differ() {
    // The goldens would not catch a kernel swap if all orders agreed.
    let g: Value = serde_json::from_str(GOLDEN).unwrap();
    let at = |k: Kernel, n: u64| {
        g["kernels"][k.name()]["sdot"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["n"] == n)
            .unwrap()["xy"]
            .clone()
    };
    for (a, b) in [
        (Kernel::Haswell, Kernel::SkylakeX),
        (Kernel::Sandybridge, Kernel::Haswell),
        (Kernel::Nehalem, Kernel::Sandybridge),
        (Kernel::Prescott, Kernel::Nehalem),
    ] {
        let differ = [1000, 1024, 1087, 1088, 2048]
            .iter()
            .any(|&n| at(a, n) != at(b, n));
        assert!(differ, "{a:?} and {b:?} agree on every probe");
    }
}

#[test]
fn shorter_operand_sets_n_and_empty_is_zero() {
    let x = sdot_input(3, 70);
    let y = sdot_input(4, 40);
    for k in Kernel::ALL {
        assert_eq!(
            sdot_with(k, &x, &y).to_bits(),
            sdot_with(k, &x[..40], &y).to_bits()
        );
        assert_eq!(sdot_with(k, &[], &y).to_bits(), 0f32.to_bits());
    }
}

#[cfg(target_arch = "x86_64")]
#[test]
fn accelerated_paths_equal_the_portable_ones() {
    if !*AVX2_FMA {
        return;
    }
    for n in [32usize, 64, 96, 1024, 2048] {
        let (x, y) = (sdot_input(9, n as u64), sdot_input(10, n as u64));
        // SAFETY: `AVX2_FMA` holds.
        let (h, s) = unsafe { (haswell_avx2_fma(&x, &y), skylakex_avx2_fma(&x, &y)) };
        assert_eq!(h.to_bits(), sandy_haswell::<true>(&x, &y).to_bits(), "{n}");
        assert_eq!(s.to_bits(), skylakex(&x, &y).to_bits(), "{n}");
    }
}

#[test]
fn forced_kernel_is_per_thread_and_restored() {
    let host = active();
    with_kernel(Kernel::Prescott, || {
        assert_eq!(active(), Kernel::Prescott);
        with_kernel(Kernel::Haswell, || assert_eq!(active(), Kernel::Haswell));
        assert_eq!(active(), Kernel::Prescott);
        std::thread::spawn(move || assert_eq!(active(), host))
            .join()
            .unwrap();
    });
    assert_eq!(active(), host);
}

// ── the dispatcher ───────────────────────────────────────────────────────

/// CPUID.1:EAX for a displayed family/model (`family >= 0xf` splits into
/// base 0xf plus the extended family; the model's high nibble is exmodel).
fn eax1(family: u32, model: u32, stepping: u32) -> u32 {
    let (fam, exfam) = if family >= 0xf {
        (0xf, family - 0xf)
    } else {
        (family, 0)
    };
    stepping | (model & 0xf) << 4 | fam << 8 | (model >> 4) << 16 | exfam << 20
}

#[derive(Clone, Copy)]
enum Isa {
    Sse3,
    Avx,
    Avx2,
    /// AVX-512 (VL + OS state) without BF16.
    Avx512,
    Avx512Bf16,
    Amx,
}

fn cpu(vendor: Vendor, family: u32, model: u32, isa: Isa) -> CpuFacts {
    let lvl = isa as u8;
    CpuFacts {
        vendor,
        leaf1_eax: eax1(family, model, 0),
        leaf1_ecx: 1,
        amd_3dnow: false,
        avx: lvl >= Isa::Avx as u8,
        avx2: lvl >= Isa::Avx2 as u8,
        avx512: lvl >= Isa::Avx512 as u8,
        avx512_bf16: lvl >= Isa::Avx512Bf16 as u8,
        amx_bf16: lvl >= Isa::Amx as u8,
    }
}

#[test]
fn dynamic_c_core_table() {
    use Core::*;
    use Isa::*;
    use Vendor::{Amd, Centaur, Hygon, Intel, Zhaoxin};
    let cases: &[(CpuFacts, Core, Kernel)] = &[
        // AMD Zen 3 (the differential box, Ryzen 7 5800X3D: 19h model 21h).
        (cpu(Amd, 0x19, 0x21, Avx2), Zen, Kernel::Haswell),
        // Zen 4 (19h model 61h) with AVX512-BF16.
        (
            cpu(Amd, 0x19, 0x61, Avx512Bf16),
            Cooperlake,
            Kernel::SkylakeX,
        ),
        (cpu(Amd, 0x19, 0x61, Avx512), SkylakeX, Kernel::SkylakeX),
        // Zen 5 (1Ah, this PC's Ryzen 7 9800X3D): unknown to the table, so
        // the feature fallback.
        (
            cpu(Amd, 0x1a, 0x44, Avx512Bf16),
            Cooperlake,
            Kernel::SkylakeX,
        ),
        (cpu(Amd, 0x1a, 0x44, Avx2), Haswell, Kernel::Haswell),
        // Zen 1/2, and Zen with the OS hiding AVX.
        (cpu(Amd, 0x17, 0x71, Avx2), Zen, Kernel::Haswell),
        (cpu(Amd, 0x17, 0x71, Sse3), Barcelona, Kernel::Nehalem),
        (cpu(Hygon, 0x18, 0x01, Avx2), Zen, Kernel::Haswell),
        // Bulldozer family (15h) and K10 (10h, NULL then the fallback).
        (cpu(Amd, 0x15, 0x01, Avx), Bulldozer, Kernel::Sandybridge),
        (cpu(Amd, 0x15, 0x02, Avx), Piledriver, Kernel::Sandybridge),
        (cpu(Amd, 0x15, 0x30, Avx), Steamroller, Kernel::Sandybridge),
        (cpu(Amd, 0x15, 0x60, Avx), Excavator, Kernel::Sandybridge),
        (cpu(Amd, 0x15, 0x40, Avx), Sandybridge, Kernel::Sandybridge),
        (cpu(Amd, 0x10, 0x04, Sse3), Prescott, Kernel::Prescott),
        (cpu(Amd, 0x14, 0x01, Sse3), Bobcat, Kernel::Nehalem),
        (cpu(Amd, 0x0f, 0x41, Sse3), OpteronSse3, Kernel::Prescott),
        // Intel.
        (cpu(Intel, 6, 0x0f, Sse3), Core2, Kernel::Prescott),
        (cpu(Intel, 6, 0x1a, Sse3), Nehalem, Kernel::Nehalem),
        (cpu(Intel, 6, 0x1c, Sse3), Atom, Kernel::Nehalem),
        (cpu(Intel, 6, 0x2a, Avx), Sandybridge, Kernel::Sandybridge),
        (cpu(Intel, 6, 0x2a, Sse3), Nehalem, Kernel::Nehalem),
        (cpu(Intel, 6, 0x3c, Avx2), Haswell, Kernel::Haswell),
        (cpu(Intel, 6, 0x3c, Avx), Sandybridge, Kernel::Sandybridge),
        (cpu(Intel, 6, 0x5e, Avx2), Haswell, Kernel::Haswell),
        // Skylake-SP: BF16 parts are Cooperlake; an OS without ZMM state
        // falls to Haswell.
        (cpu(Intel, 6, 0x55, Avx512), SkylakeX, Kernel::SkylakeX),
        (
            cpu(Intel, 6, 0x55, Avx512Bf16),
            Cooperlake,
            Kernel::SkylakeX,
        ),
        (cpu(Intel, 6, 0x55, Avx2), Haswell, Kernel::Haswell),
        (cpu(Intel, 6, 0x6a, Avx512), SkylakeX, Kernel::SkylakeX),
        (cpu(Intel, 6, 0x7e, Avx512), SkylakeX, Kernel::SkylakeX),
        (cpu(Intel, 6, 0x8c, Avx512), SkylakeX, Kernel::SkylakeX),
        (cpu(Intel, 6, 0x8f, Amx), SapphireRapids, Kernel::SkylakeX),
        (
            cpu(Intel, 6, 0x8f, Avx512Bf16),
            Cooperlake,
            Kernel::SkylakeX,
        ),
        (cpu(Intel, 6, 0x97, Avx2), Haswell, Kernel::Haswell),
        (cpu(Intel, 6, 0xa7, Avx512), SkylakeX, Kernel::SkylakeX),
        // Models the table does not know take the feature fallback.
        (cpu(Intel, 6, 0xc6, Avx2), Haswell, Kernel::Haswell),
        (cpu(Intel, 6, 0xad, Amx), Cooperlake, Kernel::SkylakeX),
        // 32-bit-era cores are bumped to Prescott on x86_64.
        (cpu(Intel, 6, 0x07, Sse3), Prescott, Kernel::Prescott),
        (cpu(Intel, 0xf, 0x02, Sse3), Prescott, Kernel::Prescott),
        (cpu(Intel, 0xf, 0x04, Sse3), Prescott, Kernel::Prescott),
        // VIA/Centaur and Zhaoxin.
        (cpu(Centaur, 6, 0x0f, Sse3), Nano, Kernel::Prescott),
        (cpu(Centaur, 7, 0x5b, Avx2), Zen, Kernel::Haswell),
        (cpu(Centaur, 7, 0x5b, Avx), Dunnington, Kernel::Prescott),
        (cpu(Zhaoxin, 7, 0x5b, Avx2), Zen, Kernel::Haswell),
        (cpu(Zhaoxin, 6, 0x0f, Sse3), Nehalem, Kernel::Nehalem),
        // An unknown vendor: the fallback alone.
        (
            cpu(Vendor::Unknown, 6, 0x5e, Avx2),
            Haswell,
            Kernel::Haswell,
        ),
    ];
    for (facts, core, kernel) in cases {
        let got = dynamic_core(facts);
        assert_eq!(got, *core, "{facts:x?}");
        assert_eq!(kernel_for(got), *kernel, "{facts:x?}");
    }
}

#[test]
fn athlon_needs_3dnow() {
    let mut c = cpu(Vendor::Amd, 6, 0x08, Isa::Sse3);
    assert_eq!(get_coretype(&c), None);
    c.amd_3dnow = true;
    assert_eq!(get_coretype(&c), Some(Core::Athlon));
    assert_eq!(dynamic_core(&c), Core::Prescott);
}

#[test]
fn every_core_maps_to_a_kernel_as_the_build_aliases_it() {
    // What `OPENBLAS_CORETYPE=<name>` reported in numpy 2.4 (scipy-openblas
    // 0.3.31) for each core name, on Windows and Linux alike.
    use Core::*;
    let reported: &[(Core, Kernel)] = &[
        (Katmai, Kernel::Prescott),
        (Coppermine, Kernel::Prescott),
        (Northwood, Kernel::Prescott),
        (Prescott, Kernel::Prescott),
        (Banias, Kernel::Prescott),
        (Atom, Kernel::Nehalem),
        (Core2, Kernel::Prescott),
        (Penryn, Kernel::Prescott),
        (Dunnington, Kernel::Prescott),
        (Nehalem, Kernel::Nehalem),
        (Athlon, Kernel::Prescott),
        (Opteron, Kernel::Prescott),
        (OpteronSse3, Kernel::Prescott),
        (Barcelona, Kernel::Nehalem),
        (Nano, Kernel::Prescott),
        (Sandybridge, Kernel::Sandybridge),
        (Bobcat, Kernel::Nehalem),
        (Bulldozer, Kernel::Sandybridge),
        (Piledriver, Kernel::Sandybridge),
        (Haswell, Kernel::Haswell),
        (Steamroller, Kernel::Sandybridge),
        (Excavator, Kernel::Sandybridge),
        (Zen, Kernel::Haswell),
        (SkylakeX, Kernel::SkylakeX),
        (Cooperlake, Kernel::SkylakeX),
        (SapphireRapids, Kernel::SkylakeX),
    ];
    for (core, kernel) in reported {
        assert_eq!(kernel_for(*core), *kernel, "{core:?}");
    }
}

#[cfg(target_arch = "x86_64")]
#[test]
fn host_detection_agrees_with_the_cpu_features() {
    let facts = host_facts();
    let k = detect();
    assert_eq!(k, *HOST);
    let has = |f: bool, what: &str| assert!(f, "{k:?} chosen without {what}");
    match k {
        Kernel::SkylakeX => has(std::arch::is_x86_feature_detected!("avx512vl"), "AVX512VL"),
        Kernel::Haswell => has(std::arch::is_x86_feature_detected!("avx2"), "AVX2"),
        Kernel::Sandybridge => has(std::arch::is_x86_feature_detected!("avx"), "AVX"),
        Kernel::Nehalem | Kernel::Prescott => {}
    }
    assert_eq!(facts.avx, std::arch::is_x86_feature_detected!("avx"));
    assert_eq!(facts.avx2, std::arch::is_x86_feature_detected!("avx2"));
}
