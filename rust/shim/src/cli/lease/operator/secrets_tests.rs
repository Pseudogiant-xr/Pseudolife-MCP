//! Exact DB-free vectors from tests/test_coordination_secrets.py.
use super::refused;

#[test]
fn original_secret_and_ordinary_vectors_match() {
    let secrets = [
        (
            "github_token",
            [
                "use ", "ghp_", "Ab1C", "d2Ef", "3Gh4", "Ij5K", "l6Mn", "7Op8", "Qr9S", "t0Uv",
                "1Wx2", " for", " the", " pus", "h",
            ]
            .concat(),
        ),
        (
            "github_token",
            [
                "gho_", "Zz9Y", "y8Xx", "7Ww6", "Vv5U", "u4Tt", "3Ss2", "Rr1Q", "q0Pp", "9Oo8",
            ]
            .concat(),
        ),
        (
            "github_pat",
            [
                "GITH", "UB_T", "OKEN", " is ", "gith", "ub_p", "at_1", "1AB2", "CD3E", "F4GH",
                "5IJ6", "KL7M", "N_q8", "Rs9T", "u0Vw", "1Xy2", "Za3B", "c4De", "5Fg6", "Hi7J",
                "k8Lm", "9No0",
            ]
            .concat(),
        ),
        (
            "anthropic_key",
            [
                "key ", "sk-a", "nt-a", "pi03", "-Xy9", "_Kq2", "-Lm7", "Pz4R", "t6Wv", "8Bc3",
                "Dd1E", "e5Ff", "0Gg ", "ok?",
            ]
            .concat(),
        ),
        (
            "openai_key",
            [
                "sk-a", "1B2c", "3D4e", "5F6g", "7H8i", "9J0k", "1L2m", "3N4o", "5P6q", "7R8s",
                "9T0u", "1V2",
            ]
            .concat(),
        ),
        (
            "openai_key",
            [
                "(sk-", "proj", "-Zq9", "-Wd4", "_Rk7", "Pm2X", "s8Lt", "3Nv6", "Hb1J", "c5Gf",
                "0Ky9", "Ua4I", "e7Ow", "2)",
            ]
            .concat(),
        ),
        (
            "aws_access_key_id",
            ["id=A", "KIAQ", "3R7T", "2W9Y", "4U8P", "6L1"].concat(),
        ),
        (
            "aws_access_key_id",
            ["ASIA", "Z9X8", "C7V6", "B5N4", "M3K2"].concat(),
        ),
        (
            "aws_secret_key",
            [
                "aws_", "secr", "et_a", "cces", "s_ke", "y=wJ", "alrX", "UtnF", "EMI/", "K7MD",
                "ENG/", "bPxR", "fiCY", "EXAM", "PLEK", "EY",
            ]
            .concat(),
        ),
        (
            "aws_secret_key",
            [
                "AWS_", "SECR", "ET_A", "CCES", "S_KE", "Y: '", "wJal", "rXUt", "nFEM", "I/K7",
                "MDEN", "G/bP", "xRfi", "CYEX", "AMPL", "EKEY", "'",
            ]
            .concat(),
        ),
        (
            "slack_token",
            [
                "bot ", "xoxb", "-123", "4567", "8901", "2-12", "3456", "7890", "123-", "AbCd",
                "EfGh", "IjKl", "MnOp", "QrSt", "UvWx",
            ]
            .concat(),
        ),
        (
            "huggingface_token",
            [
                "HF_T", "OKEN", " is ", "hf_A", "bCdE", "fGhI", "jKlM", "nOpQ", "rStU", "vWxY",
                "z012", "3456", "7",
            ]
            .concat(),
        ),
        (
            "stripe_key",
            [
                "sk_l", "ive_", "51HA", "bCdE", "fGh2", "IjKl", "3MnO", "p4Qr", "St",
            ]
            .concat(),
        ),
        (
            "google_api_key",
            [
                "maps", " key", " AIz", "aSyA", "bCdE", "fGh1", "IjKl", "Mn2O", "pQrS", "t3Uv",
                "WxYz", "4AbC",
            ]
            .concat(),
        ),
        (
            "gitlab_token",
            ["glpa", "t-Ab", "1Cd2", "Ef3G", "h4Ij", "5Kl6", "Mn"].concat(),
        ),
        (
            "jwt",
            [
                "Auth", "oriz", "atio", "n: B", "eare", "r ey", "JhbG", "ciOi", "JIUz", "I1Ni",
                "J9.e", "yJzd", "WIiO", "iIxM", "jM0N", "TY3O", "DkwI", "n0.d", "ozjg", "NryP",
                "4J3j", "VmNH", "l0w5", "N_Xg", "L0n3", "I9Pl", "FUP0", "THsR", "8U",
            ]
            .concat(),
        ),
        (
            "bearer_token",
            [
                "Auth", "oriz", "atio", "n: B", "eare", "r q7", "Hd2k", "Lm9P", "z4Rt", "6Wv8",
                "Xy1B", "c3Ns", "5Jf0", "Ge",
            ]
            .concat(),
        ),
        (
            "dsn_password",
            [
                "conn", "ect ", "with", " pos", "tgre", "sql:", "//ps", "eudo", ":Zq8", "wKd3",
                "mRt@", "127.", "0.0.", "1:54", "33/d", "b",
            ]
            .concat(),
        ),
        (
            "private_key",
            [
                "----", "-BEG", "IN R", "SA P", "RIVA", "TE K", "EY--", "---\n", "MIIE", "ow..",
                ".",
            ]
            .concat(),
        ),
        (
            "private_key",
            ["----", "-BEG", "IN P", "RIVA", "TE K", "EY--", "---"].concat(),
        ),
        (
            "private_key",
            [
                "----", "-BEG", "IN O", "PENS", "SH P", "RIVA", "TE K", "EY--", "---",
            ]
            .concat(),
        ),
        (
            "private_key",
            [
                "----", "-BEG", "IN P", "GP P", "RIVA", "TE K", "EY B", "LOCK", "----", "-",
            ]
            .concat(),
        ),
        (
            "cli_flag",
            [
                "pseu", "doli", "fe-m", "cp s", "him ", "--to", "ken ", "q7Hd", "2kLm", "9Pz4",
                "Rt6W", "v8Xy", "1Bc3", "Ns5J", "f0Ge",
            ]
            .concat(),
        ),
        (
            "cli_flag",
            [
                "--ap", "i-ke", "y 'q", "7Hd2", "kLm9", "Pz4R", "t6Wv", "8Xy1", "Bc3N", "s5Jf",
                "0Ge'",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "PSEU", "DOLI", "FE_M", "CP_T", "OKEN", "=q7H", "d2kL", "m9Pz", "4Rt6", "Wv8X",
                "y1Bc", "3Ns5", "Jf0G", "e",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "PSEU", "DOLI", "FE_M", "CP_T", "OKEN", "S=co", "dex:", "q7Hd", "2kLm", "9Pz4",
                "Rt6W", "v8Xy", "1Bc3", "Ns5J", "f0Ge", ",cla", "ude:", "Lx2e", "G0fJ", "5sN3",
                "cB1y", "X8vW", "6tR4", "zP9m", "Lk2d", "H7q",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "X-PL", "-Age", "nt-K", "ey: ", "q7Hd", "2kLm", "9Pz4", "Rt6W", "v8Xy", "1Bc3",
                "Ns5J", "f0Ge",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "priv", "ate_", "key=", "q7Hd", "2kLm", "9Pz4", "Rt6W", "v8Xy", "1Bc3", "Ns5J",
                "f0Ge",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "acce", "ss_k", "ey=q", "7Hd2", "kLm9", "Pz4R", "t6Wv", "8Xy1", "Bc3N", "s5Jf",
                "0Ge",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "auth", "=q7H", "d2kL", "m9Pz", "4Rt6", "Wv8X", "y1Bc", "3Ns5", "Jf0G", "e",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "{\"ap", "i_ke", "y\": ", "\"q7H", "d2kL", "m9Pz", "4Rt6", "Wv8X", "y1Bc", "3Ns5",
                "Jf0G", "e\"}",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "pass", "word", ": Co", "rrec", "t7Ho", "rse9", "Batt", "ery2",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "clie", "nt_s", "ecre", "t = ", "'q7H", "d2kL", "m9Pz", "4Rt6", "Wv8X", "y1Bc",
                "3Ns5", "Jf0G", "e'",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "x-ap", "i-ke", "y: q", "7Hd2", "kLm9", "Pz4R", "t6Wv", "8Xy1", "Bc3N", "s5Jf",
                "0Ge",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "apiK", "ey=q", "7Hd2", "kLm9", "Pz4R", "t6Wv", "8Xy1", "Bc3N", "s5Jf", "0Ge",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "pass", "wd=q", "7Hd2", "kLm9", "Pz4R", "t6Wv", "8Xy1", "Bc3N", "s5Jf", "0Ge",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "cred", "enti", "als:", "\tq7H", "d2kL", "m9Pz", "4Rt6", "Wv8X", "y1Bc", "3Ns5",
                "Jf0G", "e",
            ]
            .concat(),
        ),
        (
            "key_value",
            [
                "api_", "key=", "k3v9", "x2m7", "q8w1", "z5r4", "t6y0", "u2i8", "o4p7", "a1s3",
                "d5f9",
            ]
            .concat(),
        ),
    ];
    for (index, (_, text)) in secrets.iter().enumerate() {
        assert!(refused(text), "secret vector {index}");
    }
    let ordinary = [
        [
            "I ro", "tate", "d th", "e to", "ken;", " the", " sec", "ret ", "now ", "live", "s in",
            " the", " vau", "lt, ", "not ", "here", ".",
        ]
        .concat(),
        [
            "toke", "n bu", "dget", ": co", "re 1", "1,49", "5 of", " 11,", "500 ", "char", "s",
        ]
        .concat(),
        [
            "Pass", " the", " pas", "swor", "d pr", "ompt", ", th", "en p", "aste", " not", "hing",
            ": to", "kens", " and", " sec", "rets", " sta", "y lo", "cal.",
        ]
        .concat(),
        [
            "merg", "ed 5", "0578", "2936", "4fdb", "f9e2", "ebfe", "007d", "7722", "c300", "1bfd",
            "471 ", "onto", " mas", "ter",
        ]
        .concat(),
        [
            "sha2", "56=9", "f86d", "0818", "84c7", "d659", "a2fe", "aa0c", "55ad", "015a", "3bf4",
            "f1b2", "b0b8", "22cd", "15d6", "c15b", "0f00", "a08",
        ]
        .concat(),
        [
            "cred", "enti", "al_h", "ash:", " 9f8", "6d08", "1884", "c7d6", "59a2", "feaa", "0c55",
            "ad01", "5a3b", "f4f1", "b2b0", "b822", "cd15", "d6c1", "5b0f", "00a0", "8",
        ]
        .concat(),
        [
            "toke", "n=50", "5782", "9364", "fdbf", "9e2e", "bfe0", "07d7", "722c", "3001", "bfd4",
            "71",
        ]
        .concat(),
        [
            "sess", "ion ", "123e", "4567", "-e89", "b-12", "d3-a", "456-", "4266", "1417", "4000",
            " clo", "sed",
        ]
        .concat(),
        [
            "sess", "ion_", "toke", "n=12", "3e45", "67-e", "89b-", "12d3", "-a45", "6-42", "6614",
            "1740", "00",
        ]
        .concat(),
        [
            "acke", "d me", "ssag", "e 01", "2345", "6789", "abcd", "ef01", "2345", "6789", "abcd",
            "ef f", "rom ", "agen", "t fe", "dcba", "9876", "5432", "10fe", "dcba", "9876", "5432",
            "10",
        ]
        .concat(),
        [
            "agen", "t_ke", "y=01", "2345", "6789", "abcd", "ef01", "2345", "6789", "abcd", "ef",
        ]
        .concat(),
        [
            "api_", "key=", "0123", "4567", "89AB", "CDEF", "0123", "4567", "89AB", "CDEF",
        ]
        .concat(),
        ["PSEU", "DOLI", "FE_M", "CP_T", "OKEN", "=<be", "arer", ">"].concat(),
        [
            "toke", "n: P", "SEUD", "OLIF", "E_MC", "P_TO", "KEN_", "FILE",
        ]
        .concat(),
        [
            "PSEU", "DOLI", "FE_M", "CP_T", "OKEN", "=xxx", "xxxx", "xxxx", "xxxx", "xxxx", "xxxx",
            "x",
        ]
        .concat(),
        [
            "pass", "word", "_fil", "e=/r", "un/s", "ecre", "ts/d", "b_pa", "sswo", "rd_2", "026_",
            "prod",
        ]
        .concat(),
        [
            "toke", "n_fi", "le=C", ":/Us", "ers/", "exam", "ple/", "toke", "n.tx", "t",
        ]
        .concat(),
        [
            "max_", "toke", "ns=4", "096,", " tok", "ens:", " 123", "4567", "8901", "2345", "6789",
            "0123", "4",
        ]
        .concat(),
        [
            "toke", "nize", "r=al", "l-Mi", "niLM", "-L6-", "v2-o", "nnx-", "quan", "tize", "d-in",
            "t8",
        ]
        .concat(),
        [
            "stat", "us: ", "suit", "e=ru", "nnin", "g; s", "ecre", "t_li", "ke_b", "ody=", "0 of",
            " 816",
        ]
        .concat(),
        [
            "cred", "enti", "al =", " sec", "rets", ".tok", "en_u", "rlsa", "fe(3", "2)",
        ]
        .concat(),
        [
            "toke", "n_ma", "p=pa", "rse_", "toke", "n_ma", "p(os", ".env", "iron", ".get", "('PS",
            "EUDO", "LIFE", "_MCP", "_TOK", "ENS'", "))",
        ]
        .concat(),
        [
            "keys", " sta", "rt w", "ith ", "sk-a", "nt- ", "or g", "hp_ ", "and ", "PEM ", "file",
            "s wi", "th B", "EGIN", " PRI", "VATE", " KEY",
        ]
        .concat(),
        [
            "the ", "task", "-a1B", "2c3D", "4e5F", "6g7H", "8i9J", "0k1L", "2m3N", "4o5P", "6q7 ",
            "bran", "ch",
        ]
        .concat(),
        [
            "risk", "-a1B", "2c3D", "4e5F", "6g7H", "8i9J", "0k1L", "2m3N", "4o5P", "6q7 ", "note",
        ]
        .concat(),
        [
            "sk-l", "earn", "-sty", "le-e", "stim", "ator", "-wra", "pper", "-for", "-the", "-ben",
            "ch",
        ]
        .concat(),
        [
            "xoxb", "-tok", "en-p", "lace", "hold", "er-i", "n-th", "e-do", "cs",
        ]
        .concat(),
        [
            "gith", "ub_p", "at_t", "oken", "s_ar", "e_re", "fuse", "d_by", "_the", "_boa", "rd",
        ]
        .concat(),
        [
            "secr", "ets-", "scan", ": cl", "aude", "/ela", "ted-", "bart", "ik-b", "cc1a", "4",
        ]
        .concat(),
        [
            "toke", "n-fi", "x: f", "eat/", "v46-", "reda", "ctab", "le-b", "odie", "s",
        ]
        .concat(),
        [
            "toke", "n-li", "mit:", " exa", "mple", "/cla", "ude/", "bran", "ch-n", "ame-", "2026",
        ]
        .concat(),
        [
            "cred", "enti", "al_f", "ile=", "secr", "ets/", "2026", "/ben", "ch_p", "g.tx", "t",
        ]
        .concat(),
        [
            "cred", "enti", "al_h", "ash_", "pref", "ix=9", "f86d", "0818", "84c7", "d659", "a2fe",
            "aa0",
        ]
        .concat(),
        [
            "cred", "enti", "al_h", "ash=", "sha2", "56:9", "f86d", "0818", "84c7", "d659", "a2fe",
            "aa0c", "55ad", "015a", "3bf4", "f1b2", "b0b8", "22cd", "15d6", "c15b", "0f00", "a08",
        ]
        .concat(),
        [
            "toke", "n_di", "gest", "=cf8", "3e13", "57ee", "fb8b", "df15", "4285", "0d66", "d800",
            "7d62", "0e40", "50b5", "715d", "c83f", "4a92", "1d36", "ce9c", "e47d", "0d13", "c5d8",
            "5f2b", "0ff8", "318d", "2877", "eec2", "f63b", "931b", "d474", "17a8", "1a53", "8327",
            "af92", "7da3", "e",
        ]
        .concat(),
        [
            "toke", "n=sh", "a256", "-47D", "EQpj", "8HBS", "a+/T", "ImW+", "5JCe", "uQeR", "km5N",
            "MpJW", "ZG3h", "SuFU", "=",
        ]
        .concat(),
        [
            "inte", "grit", "y: s", "ha38", "4-oq", "VuAf", "XRKa", "p7fd", "gcCY", "5uyk", "M6Kz",
            "4yPz", "I4aW", "2yF3", "Q9cG", "o",
        ]
        .concat(),
        [
            "sess", "ion_", "toke", "n=12", "3e45", "67-e", "89b-", "12d3", "-a45", "6-42", "6614",
            "1740", "00-2",
        ]
        .concat(),
        [
            "toke", "n_so", "urce", "=rea", "dThe", "Toke", "nFro", "mThe", "Envi", "ronm", "entV",
            "aria", "bleN", "ow",
        ]
        .concat(),
        [
            "toke", "nise", "r=se", "nten", "cepi", "ece-", "Mode", "l202", "6-Va", "rian", "t7",
        ]
        .concat(),
        [
            "sk-l", "earn", "-v2-", "esti", "mato", "r-wr", "appe", "r-fo", "r-th", "e-be", "nch-",
            "2026",
        ]
        .concat(),
        [
            "/tmp", "/sk-", "0123", "4567", "89ab", "cdef", "0123", "4567", "89ab", "cdef", "0123",
            "4567",
        ]
        .concat(),
        [
            "logs", ".sk-", "a1b2", "c3d4", "e5f6", "a7b8", "c9d0", "e1f2", "a3b4", "c5d6", "e7f8",
        ]
        .concat(),
        ["ASIA", "PACI", "FICS", "ALES", "TEAM"].concat(),
        [
            "ghs_", "abcd", "efgh", "ijkl", "mnop", "qrst", "uvwx", "yz01", "2345", "6789",
        ]
        .concat(),
        [
            "gith", "ub_p", "at_r", "otat", "ion_", "2026", "_q3_", "foll", "owup", "_not", "e",
        ]
        .concat(),
        ["auth", "or: ", "Pseu", "dogi", "ant-", "xr"].concat(),
        [
            "cach", "e_ke", "y=me", "mory", "_age", "nts_", "2026", "_09_", "26_r", "oste", "r",
        ]
        .concat(),
        [
            "cach", "e_ke", "y=me", "mory", "_age", "nts_", "2026", "_09_", "26_r", "oste", "r_lo",
            "ng_v", "ersi", "on",
        ]
        .concat(),
        [
            "sort", "_key", ": cr", "eate", "d_at", "_202", "6_09", "_26_", "then", "_age", "nt_i",
            "d_as", "c_v2",
        ]
        .concat(),
        [
            "idem", "pote", "ncy_", "key=", "send", "-202", "6-09", "-26-", "rela", "y-6-", "retr",
            "y-3-", "of-5",
        ]
        .concat(),
        [
            "keyb", "oard", "=us-", "intl", "-202", "6-la", "yout", "-wit", "h-de", "ad-k", "eys",
        ]
        .concat(),
        [
            "sk_l", "ive_", "xxxx", "xxxx", "xxxx", "xxxx", "xxxx", "xxxx",
        ]
        .concat(),
        [
            "set ", "PSEU", "DOLI", "FE_M", "CP_T", "OKEN", "S=ed", "itor", ":<to", "ken>", ",rev",
            "iewe", "r:<t", "oken", ">",
        ]
        .concat(),
        [
            "Auth", "oriz", "atio", "n: B", "eare", "r <P", "SEUD", "OLIF", "E_MC", "P_TO", "KEN>",
        ]
        .concat(),
        [
            "Auth", "oriz", "atio", "n: B", "eare", "r $P", "SEUD", "OLIF", "E_MC", "P_TO", "KEN",
        ]
        .concat(),
        [
            "post", "gres", "ql:/", "/pse", "udol", "ife:", "<pas", "swor", "d>@1", "27.0", ".0.1",
            ":543", "3/ps", "eudo", "life",
        ]
        .concat(),
        [
            "post", "gres", "ql:/", "/pse", "udol", "ife:", "${PO", "STGR", "ES_P", "ASSW", "ORD}",
            "@db:", "5432", "/pse", "udol", "ife",
        ]
        .concat(),
        [""].concat(),
    ];
    for (index, text) in ordinary.iter().enumerate() {
        assert!(!refused(text), "ordinary vector {index}");
    }
}
