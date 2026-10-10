#![forbid(unsafe_code)]
//! Archive verification across every event family the coordination store
//! writes, without a bank. The archives are the Python oracle's own
//! `board-audit export --out` of disposable banks seeded through
//! `CoordinationStore` (rust/cli_harness/rows/audit.py); the expected reports
//! were printed by the oracle's `board-audit verify --input` on the same
//! files and edits. Synthetic agents and bodies only.
#[path = "common/cli.rs"]
mod cli;
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

// Register, update (status, park), attach, detach, send (direct, clears,
// project and board fan-out, urgent), read, ack, lease acquire/queue/
// release/grant/break/delegate/expire, woke, redact, expire, a pre-v46
// legacy send and a v45-shaped send.
const RICH: &[&str] = &[
    r##"{"seq":1,"event":"register","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"capabilities":{},"episode":"","label":"claude-code","project":"p","status":"starting \u2713","task":"t1","wake_enabled":true},"created_at":1700000000.0,"hlc":"","prev_hash":"0000000000000000000000000000000000000000000000000000000000000000","hash":"0510a174eb822b94c679a1e07c13c72f7cefc70f899f1136728183f2c3881a43","body":null,"body_salt":null}"##,
    r##"{"seq":2,"event":"register","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"p","task":"t2","message_id":null,"payload":{"capabilities":{},"episode":"","label":"codex","project":"p","status":"","task":"t2","wake_enabled":true},"created_at":1700000007.25,"hlc":"","prev_hash":"0510a174eb822b94c679a1e07c13c72f7cefc70f899f1136728183f2c3881a43","hash":"f5ef27664dfa4849fa72c65f24cd5e07298a2f98b579fb4e8cff75177ea80533","body":null,"body_salt":null}"##,
    r##"{"seq":3,"event":"register","actor":"agent","principal":"bob","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":null,"project":"q","task":"t3","message_id":null,"payload":{"capabilities":{},"episode":"","label":"","name":"third","name_source":"agent","project":"q","status":"","task":"t3","wake_enabled":false},"created_at":1700000014.5,"hlc":"","prev_hash":"f5ef27664dfa4849fa72c65f24cd5e07298a2f98b579fb4e8cff75177ea80533","hash":"0d46b4480acec2c63f55e83a2493968d3178d10a6bd894a35f6cddfd1f0195f5","body":null,"body_salt":null}"##,
    r##"{"seq":4,"event":"update","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"before":{"status":"starting \u2713","status_expires_at":null},"fields":{"status":"suite=queued #3","status_expires_at":1700000621.75}},"created_at":1700000021.75,"hlc":"","prev_hash":"0d46b4480acec2c63f55e83a2493968d3178d10a6bd894a35f6cddfd1f0195f5","hash":"29d7f7d055a983aec19702fafbc1989c4f91e567a82a80877316c0a976d40616","body":null,"body_salt":null}"##,
    r##"{"seq":5,"event":"update","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"p","task":"t2","message_id":null,"payload":{"before":{"park_expires":null,"park_needs":"","park_reason":null,"park_set_at":null,"status":""},"fields":{"park_expires":1700043229.0,"park_needs":"the gpu","park_reason":"needs_resource","park_set_at":1700000029.0,"status":"waiting for the gpu"}},"created_at":1700000029.0,"hlc":"","prev_hash":"29d7f7d055a983aec19702fafbc1989c4f91e567a82a80877316c0a976d40616","hash":"53c4a9c9d065afcb1bed55ce2bb003a08c910f675e0c4f293322041da3425a17","body":null,"body_salt":null}"##,
    r##"{"seq":6,"event":"attach","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"p","task":"t2","message_id":null,"payload":{"generation":1,"lease_until":1700000096.25,"renewed":false,"wake_enabled":true},"created_at":1700000036.25,"hlc":"","prev_hash":"53c4a9c9d065afcb1bed55ce2bb003a08c910f675e0c4f293322041da3425a17","hash":"d593cfa1cfcbacf0b089ddb386c27a28c51980d3aff7e45ba95011e5e49a966b","body":null,"body_salt":null}"##,
    r##"{"seq":7,"event":"attach","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"generation":1,"lease_until":1700000096.25,"renewed":false,"wake_enabled":false},"created_at":1700000036.25,"hlc":"","prev_hash":"d593cfa1cfcbacf0b089ddb386c27a28c51980d3aff7e45ba95011e5e49a966b","hash":"8fdc3c0c385927951b6ed1308f4e1f184063e168d328e85c102c33d4eadb6d63","body":null,"body_salt":null}"##,
    r##"{"seq":8,"event":"send","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"1f3876bb99b946f7a0842b60e7ee99c9","payload":{"expires_at":1700086443.5,"recipient_sequence":1,"reply_to":null,"request_id":"r1","text_commitment":"63a5baf4e7186aed7b8bb110dcb4b2755c1ac31040350f1238eb6addfcc0f797","wake":"withheld"},"created_at":1700000043.5,"hlc":"","prev_hash":"8fdc3c0c385927951b6ed1308f4e1f184063e168d328e85c102c33d4eadb6d63","hash":"d2390d4da52cc73ab53bfe17c84cd3a751387895bc47e0d5f6131880c9e5b57b","body":"from t1 na\u00efve \u2713 \"quoted\"","body_salt":"e863890bf1d13b88769c8477efdce03c"}"##,
    r##"{"seq":9,"event":"send","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"55b086aa4c9248eca1b5054deaf961d8","payload":{"expires_at":1700086444.0,"recipient_sequence":2,"reply_to":null,"request_id":"r2","text_commitment":"e220855cd0a4f0292815915d58b62aebd857015a9525a9169d55e8d541df2866","wake":"withheld"},"created_at":1700000044.0,"hlc":"","prev_hash":"d2390d4da52cc73ab53bfe17c84cd3a751387895bc47e0d5f6131880c9e5b57b","hash":"2e8b5cf6bc128300f2b3aef5de376a8abb5f2090f7d308067b3538ded5ab5229","body":"the gpu is free","body_salt":"8d60070602fbcf8fa84aed9efa9c75ed"}"##,
    r##"{"seq":10,"event":"send","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"23eef6a5449a4d07a02dcbfac1921529","payload":{"expires_at":1700086451.25,"fanout":{"recipients":1,"to":"project:p"},"recipient_sequence":3,"reply_to":null,"request_id":"burst","text_commitment":"b373274d25c1b7444efcbf3072944b7b061d8621af940bd86fe75292ec169100","wake":"withheld"},"created_at":1700000051.25,"hlc":"","prev_hash":"2e8b5cf6bc128300f2b3aef5de376a8abb5f2090f7d308067b3538ded5ab5229","hash":"df101ce20aceb267c4ecab5c8f9aa1fc5c3dbb18fa9095213bb8594559d9f555","body":"to the whole project","body_salt":"15b284d23d1e44cfc4f79d70b6afc617"}"##,
    r##"{"seq":11,"event":"send","actor":"agent","principal":"bob","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"q","task":"t3","message_id":"1b1ac667759042759be87183884297f3","payload":{"expires_at":1700086458.5,"fanout":{"recipients":2,"to":"all"},"recipient_sequence":4,"reply_to":null,"request_id":"hello","text_commitment":"cac99672dffa0f8d3efc2d5b7f84c01986c637a2bd5f21e79c64d7db106b5ef6","wake":"rung","wake_reason":"urgent"},"created_at":1700000058.5,"hlc":"","prev_hash":"df101ce20aceb267c4ecab5c8f9aa1fc5c3dbb18fa9095213bb8594559d9f555","hash":"ef2ac59ea03d783aba3bc8e66d5d94063b3ffdde73d3358cf7b9caa9bfdb2bb4","body":"hello everyone","body_salt":"74123d567d8662d69ebe8b1ab354e60d"}"##,
    r##"{"seq":12,"event":"send","actor":"agent","principal":"bob","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":"5bd4c853eb8847e69281486d360debbc","project":"q","task":"t3","message_id":"6fc819e5dc57430581499de160aa481f","payload":{"expires_at":1700086458.5,"fanout":{"recipients":2,"to":"all"},"recipient_sequence":1,"reply_to":null,"request_id":"hello","text_commitment":"0e8215a3c849491aa6f1fb2257ced4064cb1dd3e948f3ae119f622930ff2dbde","wake":"hinted"},"created_at":1700000058.5,"hlc":"","prev_hash":"ef2ac59ea03d783aba3bc8e66d5d94063b3ffdde73d3358cf7b9caa9bfdb2bb4","hash":"3efd1e52cac41d6635c6ea8c2803462df41b2f93577757d7f8b70039cf09324f","body":"hello everyone","body_salt":"1258cbdeb9e6e2738420b54438662f0e"}"##,
    r##"{"seq":13,"event":"read","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"1f3876bb99b946f7a0842b60e7ee99c9","payload":{"path":"pull","sender_agent_id":"5bd4c853eb8847e69281486d360debbc"},"created_at":1700000065.75,"hlc":"","prev_hash":"3efd1e52cac41d6635c6ea8c2803462df41b2f93577757d7f8b70039cf09324f","hash":"cbe7635964d5f72241a4994b97cce4f6d03726353599f4f5e22a76b8b1bb4931","body":null,"body_salt":null}"##,
    r##"{"seq":14,"event":"read","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"55b086aa4c9248eca1b5054deaf961d8","payload":{"path":"pull","sender_agent_id":"5bd4c853eb8847e69281486d360debbc"},"created_at":1700000065.75,"hlc":"","prev_hash":"cbe7635964d5f72241a4994b97cce4f6d03726353599f4f5e22a76b8b1bb4931","hash":"c85af299f765eeca47900dc6dc722dfac9cbef25ca72690fb1062a5a13d2b230","body":null,"body_salt":null}"##,
    r##"{"seq":15,"event":"read","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"23eef6a5449a4d07a02dcbfac1921529","payload":{"path":"pull","sender_agent_id":"5bd4c853eb8847e69281486d360debbc"},"created_at":1700000065.75,"hlc":"","prev_hash":"c85af299f765eeca47900dc6dc722dfac9cbef25ca72690fb1062a5a13d2b230","hash":"025b2dbad97750fb6117ca206535e60cf54003e424769fffd797edc67864dbbf","body":null,"body_salt":null}"##,
    r##"{"seq":16,"event":"read","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"q","task":"t3","message_id":"1b1ac667759042759be87183884297f3","payload":{"path":"pull","sender_agent_id":"f479f33dea6247949e82e476e6639040"},"created_at":1700000065.75,"hlc":"","prev_hash":"025b2dbad97750fb6117ca206535e60cf54003e424769fffd797edc67864dbbf","hash":"eddc31203d8693c44273b9507d3ed3c7a2941b03ad8c8e2ad31dc6bae9c4110f","body":null,"body_salt":null}"##,
    r##"{"seq":17,"event":"ack","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"1f3876bb99b946f7a0842b60e7ee99c9","payload":{"sender_agent_id":"5bd4c853eb8847e69281486d360debbc"},"created_at":1700000073.0,"hlc":"","prev_hash":"eddc31203d8693c44273b9507d3ed3c7a2941b03ad8c8e2ad31dc6bae9c4110f","hash":"1f09b3e7994acfbeb4e49bdea9658d2a1c0b90418c1392857e3b4ac7002689aa","body":null,"body_salt":null}"##,
    r##"{"seq":18,"event":"ack","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"55b086aa4c9248eca1b5054deaf961d8","payload":{"sender_agent_id":"5bd4c853eb8847e69281486d360debbc"},"created_at":1700000073.0,"hlc":"","prev_hash":"1f09b3e7994acfbeb4e49bdea9658d2a1c0b90418c1392857e3b4ac7002689aa","hash":"04edeaef3853ff68144c6183914e4d32556dc92323274376ce28ffdb20fcf2bd","body":null,"body_salt":null}"##,
    r##"{"seq":19,"event":"ack","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t1","message_id":"23eef6a5449a4d07a02dcbfac1921529","payload":{"sender_agent_id":"5bd4c853eb8847e69281486d360debbc"},"created_at":1700000073.0,"hlc":"","prev_hash":"04edeaef3853ff68144c6183914e4d32556dc92323274376ce28ffdb20fcf2bd","hash":"932bc0af714f21cdd2f6d113973c09db83ec52e04490f2012bd95e570b355f5a","body":null,"body_salt":null}"##,
    r##"{"seq":20,"event":"ack","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"q","task":"t3","message_id":"1b1ac667759042759be87183884297f3","payload":{"sender_agent_id":"f479f33dea6247949e82e476e6639040"},"created_at":1700000073.0,"hlc":"","prev_hash":"932bc0af714f21cdd2f6d113973c09db83ec52e04490f2012bd95e570b355f5a","hash":"23e847e30fe14e992077790ac5eae629ce9fc5c6037533a7a94328401fa962f8","body":null,"body_salt":null}"##,
    r##"{"seq":21,"event":"detach","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"p","task":"t2","message_id":null,"payload":{"generation":1},"created_at":1700000073.0,"hlc":"","prev_hash":"23e847e30fe14e992077790ac5eae629ce9fc5c6037533a7a94328401fa962f8","hash":"164e5942493327d2ba17710d74650900359f579625810e64d6d74f26b87d3e2b","body":null,"body_salt":null}"##,
    r##"{"seq":22,"event":"detach","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"generation":1},"created_at":1700000073.0,"hlc":"","prev_hash":"164e5942493327d2ba17710d74650900359f579625810e64d6d74f26b87d3e2b","hash":"7a357fa0b9770ab2ffdb8364422cf1b561f411714b29ed7142112374852fbc41","body":null,"body_salt":null}"##,
    r##"{"seq":23,"event":"lease_acquire","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"expect":null,"fence":1,"name":"gpu","purpose":"bench","ttl":300},"created_at":1700000080.25,"hlc":"","prev_hash":"7a357fa0b9770ab2ffdb8364422cf1b561f411714b29ed7142112374852fbc41","hash":"db121577754028cc8a1a5299ba6ada118e37e1f02a3a19d4356dcecc52f94973","body":null,"body_salt":null}"##,
    r##"{"seq":24,"event":"lease_queue","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"p","task":"t2","message_id":null,"payload":{"expect":900,"name":"gpu","purpose":"","ttl":120},"created_at":1700000087.5,"hlc":"","prev_hash":"db121577754028cc8a1a5299ba6ada118e37e1f02a3a19d4356dcecc52f94973","hash":"d5c27f62f5d13232f263e692bbce0c5879858c573d20e51c6c529531c7549d57","body":null,"body_salt":null}"##,
    r##"{"seq":25,"event":"lease_release","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"fence":1,"name":"gpu"},"created_at":1700000094.75,"hlc":"","prev_hash":"d5c27f62f5d13232f263e692bbce0c5879858c573d20e51c6c529531c7549d57","hash":"4af9a3837a0a64ec3197b98edc507b3e9a64573194a29c254d7c64f7e5683a57","body":null,"body_salt":null}"##,
    r##"{"seq":26,"event":"lease_grant","actor":"daemon","principal":"","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"expect":900,"fence":2,"name":"gpu","purpose":"","window":120},"created_at":1700000094.75,"hlc":"","prev_hash":"4af9a3837a0a64ec3197b98edc507b3e9a64573194a29c254d7c64f7e5683a57","hash":"4cbe2c6f5f63504137bc011d01cf95cc852cd897cd753b15d69360f4c8a3a281","body":null,"body_salt":null}"##,
    r##"{"seq":27,"event":"lease_acquire","actor":"agent","principal":"bob","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":null,"project":"q","task":"t3","message_id":null,"payload":{"expect":null,"fence":3,"name":"suite","purpose":"","ttl":600},"created_at":1700000102.0,"hlc":"","prev_hash":"4cbe2c6f5f63504137bc011d01cf95cc852cd897cd753b15d69360f4c8a3a281","hash":"06e78baebf9217372c9659f987b911e68b4ee69d209c4d6ec4162374c1b66fa5","body":null,"body_salt":null}"##,
    r##"{"seq":28,"event":"lease_break","actor":"operator","principal":"","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"fence":3,"name":"suite"},"created_at":1700000102.0,"hlc":"","prev_hash":"06e78baebf9217372c9659f987b911e68b4ee69d209c4d6ec4162374c1b66fa5","hash":"1f79e2ed64a97253ce05bc4b3226dba1962e1c359f5f560eaa8ccce4afb312a6","body":null,"body_salt":null}"##,
    r##"{"seq":29,"event":"lease_delegate","actor":"operator","principal":"","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"fence":4,"hold":3600,"name":"delegate:p","replaced":null},"created_at":1700000109.25,"hlc":"","prev_hash":"1f79e2ed64a97253ce05bc4b3226dba1962e1c359f5f560eaa8ccce4afb312a6","hash":"517d75cc117c7177fb33383831842d528c9b022f9c6ec7484b2fd0ec86ad83f6","body":null,"body_salt":null}"##,
    r##"{"seq":30,"event":"woke","actor":"agent","principal":"alice","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":"539bd44fcf274f0eacac257c6057f07d","project":"p","task":"t2","message_id":null,"payload":{"rings":0},"created_at":1700000116.5,"hlc":"","prev_hash":"517d75cc117c7177fb33383831842d528c9b022f9c6ec7484b2fd0ec86ad83f6","hash":"03018cf8e2e094cd2b602a7d4c492c22d21e381cc42e16df4309a0d9f39c2932","body":null,"body_salt":null}"##,
    r##"{"seq":31,"event":"update","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"p","task":"t1","message_id":null,"payload":{"before":{"park_clear_by":"","park_expires":null,"park_needs":"","park_reason":null,"park_resume":"","park_set_at":null,"status":"suite=queued #3","status_expires_at":1700000621.75},"fields":{"park_clear_by":"","park_expires":null,"park_needs":"","park_reason":null,"park_resume":"","park_set_at":null,"status":"done for now","status_expires_at":null}},"created_at":1700000123.75,"hlc":"","prev_hash":"03018cf8e2e094cd2b602a7d4c492c22d21e381cc42e16df4309a0d9f39c2932","hash":"9ea132872eca40149dcca6025a2e3650bcc761ec97340628243f92726cb51396","body":null,"body_salt":null}"##,
    r##"{"seq":32,"event":"send","actor":"agent","principal":"bob","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":"5bd4c853eb8847e69281486d360debbc","project":"q","task":"t3","message_id":"b24a5ea1ea1f4687afeb16cfa73de098","payload":{"expires_at":1700086531.0,"recipient_sequence":2,"reply_to":null,"request_id":"oops","text_commitment":"2f0f730cf8a4550f060cd386d128053e432575c10ecb91a011303ca5b7020f7f","wake":"hinted"},"created_at":1700000131.0,"hlc":"","prev_hash":"9ea132872eca40149dcca6025a2e3650bcc761ec97340628243f92726cb51396","hash":"4937f9ba056e0c63dd2862cced4d4e85a61ef431b18de530314d956e4dc858d8","body":null,"body_salt":null}"##,
    r##"{"seq":33,"event":"redact","actor":"operator","principal":"","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":"5bd4c853eb8847e69281486d360debbc","project":"q","task":"t3","message_id":"b24a5ea1ea1f4687afeb16cfa73de098","payload":{"audit_copy":"removed","message_id":"b24a5ea1ea1f4687afeb16cfa73de098","reason":"wrong paste","seq":32},"created_at":1700000138.25,"hlc":"","prev_hash":"4937f9ba056e0c63dd2862cced4d4e85a61ef431b18de530314d956e4dc858d8","hash":"3a453dbae7b0ed556d0af04630ce37444979edc8221ff734436e251deb1e19a3","body":null,"body_salt":null}"##,
    r##"{"seq":34,"event":"lease_expire","actor":"daemon","principal":"","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"fence":4,"name":"delegate:p"},"created_at":1700086546.5,"hlc":"","prev_hash":"3a453dbae7b0ed556d0af04630ce37444979edc8221ff734436e251deb1e19a3","hash":"27c49ae44f7d7fc13d04d2da904a996560930c2ad5c91fc84815b081e3421bc1","body":null,"body_salt":null}"##,
    r##"{"seq":35,"event":"lease_expire","actor":"daemon","principal":"","agent_id":"539bd44fcf274f0eacac257c6057f07d","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"fence":2,"name":"gpu"},"created_at":1700086546.5,"hlc":"","prev_hash":"27c49ae44f7d7fc13d04d2da904a996560930c2ad5c91fc84815b081e3421bc1","hash":"cf91746bf2bd7fa58dc8740d8af3f756ab4f9e24c0dc42faeab9c76d2ae2c8aa","body":null,"body_salt":null}"##,
    r##"{"seq":36,"event":"expire","actor":"daemon","principal":"","agent_id":"","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"message_ids":["1b1ac667759042759be87183884297f3","1f3876bb99b946f7a0842b60e7ee99c9","23eef6a5449a4d07a02dcbfac1921529","55b086aa4c9248eca1b5054deaf961d8","6fc819e5dc57430581499de160aa481f"]},"created_at":1700086546.5,"hlc":"","prev_hash":"cf91746bf2bd7fa58dc8740d8af3f756ab4f9e24c0dc42faeab9c76d2ae2c8aa","hash":"ffc6da4740ee078415a7dd6807f8409e58eab3d6627cfe06b9cca864567fb7d9","body":null,"body_salt":null}"##,
    r##"{"seq":37,"event":"send","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":"f479f33dea6247949e82e476e6639040","project":"","task":"","message_id":"b5a7912dda044b27bf36e7ffaedd3314","payload":{"expires_at":1700172953.75,"recipient_sequence":1,"reply_to":null,"request_id":"legacy","text":"an old body"},"created_at":1700086553.75,"hlc":"","prev_hash":"ffc6da4740ee078415a7dd6807f8409e58eab3d6627cfe06b9cca864567fb7d9","hash":"f7caeea5a86a2b114c36ad6d81de977bc096976a9cb9078ca6f8f7701f275383","body":null,"body_salt":null}"##,
    r##"{"seq":38,"event":"send","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":"f479f33dea6247949e82e476e6639040","project":"p","task":"t1","message_id":"af431651da504749abbbb3229b63e308","payload":{"expires_at":1700172961.0,"recipient_sequence":1,"reply_to":null,"request_id":"v45","text":"sent by a v45 daemon","wake":"not_needed"},"created_at":1700086561.0,"hlc":"","prev_hash":"f7caeea5a86a2b114c36ad6d81de977bc096976a9cb9078ca6f8f7701f275383","hash":"76702fb3caf98b3c9dc5036975493193cc49e2288c68300b73b89615e44ba3fe","body":null,"body_salt":null}"##,
    r##"{"seq":39,"event":"send","actor":"agent","principal":"alice","agent_id":"5bd4c853eb8847e69281486d360debbc","recipient_agent_id":"f479f33dea6247949e82e476e6639040","project":"p","task":"t1","message_id":"dcdc06decb8d46cebcbf1bf84f815eb9","payload":{"expires_at":1700172968.25,"recipient_sequence":2,"reply_to":null,"request_id":"later","text_commitment":"00d59ec5ff9d1ef2ddac78e5f1f97c6cb9d196e64d8a4dbd42c116ffbcdbef18","wake":"not_needed"},"created_at":1700086568.25,"hlc":"","prev_hash":"76702fb3caf98b3c9dc5036975493193cc49e2288c68300b73b89615e44ba3fe","hash":"a98b6bd0b8b3647ccabad342b1a5e1a0ccd0260f124943207dc0024b3cc1cdb6","body":"redact me later","body_salt":"32e4cbf81bfd7a0f00e097545a3b64a5"}"##,
    r##"{"seq":40,"event":"update","actor":"agent","principal":"bob","agent_id":"f479f33dea6247949e82e476e6639040","recipient_agent_id":null,"project":"q","task":"t3","message_id":null,"payload":{"before":{"status":""},"fields":{"status":"after the prune"}},"created_at":1700086575.5,"hlc":"","prev_hash":"a98b6bd0b8b3647ccabad342b1a5e1a0ccd0260f124943207dc0024b3cc1cdb6","hash":"ceb6d4fda68617e3a76f52f395e1d7622795d11e99e0c621a5c4c64b09277750","body":null,"body_salt":null}"##,
];
// A chain whose oldest rows audit retention cut (`audit_prune`).
const PRUNED: &[&str] = &[
    r##"{"seq":4,"event":"update","actor":"agent","principal":"alice","agent_id":"409d79eff9a840cd83686f263494dcf4","recipient_agent_id":null,"project":"p","task":"","message_id":null,"payload":{"before":{"status":""},"fields":{"status":"still here"}},"created_at":1700259200.0,"hlc":"","prev_hash":"014081c631a6aebc6d9bc09e221375b31c8b5648c818c1727621cb77769ed183","hash":"73a33f2e82f73ab0389403aaa6e2cd064a8b64bab683c2b75551eedc73ae1b1a","body":null,"body_salt":null}"##,
    r##"{"seq":5,"event":"expire","actor":"daemon","principal":"","agent_id":"","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"message_ids":["1444f6b7b592490bb40a605ef09ebc96"]},"created_at":1700259200.0,"hlc":"","prev_hash":"73a33f2e82f73ab0389403aaa6e2cd064a8b64bab683c2b75551eedc73ae1b1a","hash":"bf930e579d334602271e3b769beef526d733292241282da506cb61425a0a206e","body":null,"body_salt":null}"##,
    r##"{"seq":6,"event":"audit_prune","actor":"daemon","principal":"","agent_id":"","recipient_agent_id":null,"project":"","task":"","message_id":null,"payload":{"cutoff":1700092800,"removed":3,"retention_days":1,"through_hash":"014081c631a6aebc6d9bc09e221375b31c8b5648c818c1727621cb77769ed183","through_seq":3},"created_at":1700259200.0,"hlc":"","prev_hash":"bf930e579d334602271e3b769beef526d733292241282da506cb61425a0a206e","hash":"0edbca146686c8f84b36bfa27699ebbffe36c9843f0158acc65edb15a2efa574","body":null,"body_salt":null}"##,
    r##"{"seq":7,"event":"update","actor":"agent","principal":"alice","agent_id":"4bad369a6777489ca5d0ebe919ccee5c","recipient_agent_id":null,"project":"p","task":"","message_id":null,"payload":{"before":{"status":""},"fields":{"status":"after the cut"}},"created_at":1700259260.0,"hlc":"","prev_hash":"0edbca146686c8f84b36bfa27699ebbffe36c9843f0158acc65edb15a2efa574","hash":"a8f826a2e458e706f9c3ee4f0651df550c7e7386cb4cc5e90c87cbfdbd99fce0","body":null,"body_salt":null}"##,
];
const RICH_REPORT: &str = r##"{"ok": true, "events": 40, "first_seq": 1, "head_seq": 40, "head_hash": "ceb6d4fda68617e3a76f52f395e1d7622795d11e99e0c621a5c4c64b09277750", "head_created_at": 1700086575.5, "start_cut": null}"##;
const PRUNED_REPORT: &str = r##"{"ok": true, "events": 4, "first_seq": 4, "head_seq": 7, "head_hash": "a8f826a2e458e706f9c3ee4f0651df550c7e7386cb4cc5e90c87cbfdbd99fce0", "head_created_at": 1700259260.0, "start_cut": {"seq": 6, "created_at": 1700259200.0, "cutoff": 1700092800, "retention_days": 1, "through_seq": 3}}"##;

fn command(home: &Path) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("board-audit")
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PSEUDOLIFE_MCP_PYTHON", "missing-interpreter");
    command
}

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("audit-chain-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn verify(&self, content: &[u8], extra: &[&str]) -> Output {
        let path = self.0.join("archive.jsonl");
        fs::write(&path, content).unwrap();
        let output = command(&self.0)
            .args(["verify", "--input"])
            .arg(&path)
            .args(extra)
            .output()
            .unwrap();
        assert_eq!(fs::read(&path).unwrap(), content);
        assert_eq!(fs::read_dir(&self.0).unwrap().count(), 1);
        output
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn joined(lines: &[&str], separator: &str) -> Vec<u8> {
    let mut text = lines.join(separator);
    text.push_str(separator);
    text.into_bytes()
}

fn rows(lines: &[&str]) -> Vec<Value> {
    lines
        .iter()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn archive(rows: &[Value]) -> Vec<u8> {
    rows.iter()
        .map(|row| serde_json::to_string(row).unwrap() + "\n")
        .collect::<String>()
        .into_bytes()
}

fn report(output: &Output, code: i32, expected: &str) {
    assert_eq!(output.status.code(), Some(code), "{output:?}");
    assert_eq!(output.stdout, cli::native_text(&format!("{expected}\n")));
    assert!(output.stderr.is_empty(), "{output:?}");
}

fn broken(output: &Output, seq: i64, reason: &str) {
    report(
        output,
        1,
        &format!("{{\"ok\": false, \"seq\": {seq}, \"reason\": \"{reason}\"}}"),
    );
}

fn send_index(rows: &[Value], body: Option<&str>) -> usize {
    rows.iter()
        .position(|row| row["event"] == "send" && row["body"].as_str() == body)
        .unwrap()
}

#[test]
fn every_family_verifies_with_the_oracle_report() {
    let home = Home::new();
    report(&home.verify(&joined(RICH, "\n"), &[]), 0, RICH_REPORT);
    report(&home.verify(&joined(PRUNED, "\n"), &[]), 0, PRUNED_REPORT);
    // Universal newlines and blank lines, as Python's text mode reads them.
    let mut odd = b"\r \n\x1c\n".to_vec();
    odd.extend(joined(RICH, "\r\r"));
    report(&home.verify(&odd, &[]), 0, RICH_REPORT);
    report(&home.verify(&joined(RICH, "\r\n"), &[]), 0, RICH_REPORT);
}

#[test]
fn expected_heads_follow_the_oracle() {
    let home = Home::new();
    let all = rows(RICH);
    let last = all.last().unwrap();
    let head = format!("{}:{}", last["seq"], last["hash"].as_str().unwrap());
    report(
        &home.verify(&joined(RICH, "\n"), &["--expect-head", &head]),
        0,
        RICH_REPORT,
    );
    let earlier = format!("3:{}", all[2]["hash"].as_str().unwrap());
    report(
        &home.verify(&joined(RICH, "\n"), &["--expect-head", &earlier]),
        0,
        RICH_REPORT,
    );
    let mismatch = format!("{}:{}", last["seq"], "0".repeat(64));
    broken(
        &home.verify(&joined(RICH, "\n"), &["--expect-head", &mismatch]),
        40,
        "head_mismatch",
    );
    let missing = format!("9999:{}", last["hash"].as_str().unwrap());
    broken(
        &home.verify(&joined(RICH, "\n"), &["--expect-head", &missing]),
        9999,
        "head_missing",
    );
    let pruned = format!("1:{}", "a".repeat(64));
    report(
        &home.verify(&joined(PRUNED, "\n"), &["--expect-head", &pruned]),
        1,
        concat!(
            "{\"ok\": false, \"seq\": 1, \"reason\": \"head_pruned\", \"start_cut\": ",
            "{\"seq\": 6, \"created_at\": 1700259200.0, \"cutoff\": 1700092800, ",
            "\"retention_days\": 1, \"through_seq\": 3}}"
        ),
    );
}

#[test]
fn body_rules_name_the_oracle_reasons() {
    let home = Home::new();
    let all = rows(RICH);
    let target = send_index(&all, Some("redact me later"));
    let edit = |change: &dyn Fn(&mut Value)| {
        let mut edited = all.clone();
        change(&mut edited[target]);
        archive(&edited)
    };
    broken(
        &home.verify(
            &edit(&|row| {
                row["body"] = Value::Null;
                row["body_salt"] = Value::Null;
            }),
            &[],
        ),
        39,
        "body_missing",
    );
    broken(
        &home.verify(&edit(&|row| row["body"] = "another body".into()), &[]),
        39,
        "body_mismatch",
    );
    broken(
        &home.verify(&edit(&|row| row["body_salt"] = "0".repeat(32).into()), &[]),
        39,
        "body_mismatch",
    );
    broken(
        &home.verify(&edit(&|row| row["body"] = Value::Null), &[]),
        39,
        "body_mismatch",
    );
    let stripped: Vec<Value> = all
        .iter()
        .map(|row| {
            let mut row = row.clone();
            let object = row.as_object_mut().unwrap();
            object.remove("body");
            object.remove("body_salt");
            row
        })
        .collect();
    broken(
        &home.verify(&archive(&stripped), &[]),
        8,
        "body_not_exported",
    );
    // A redacted send's body written back after its redaction.
    let mut kept = all.clone();
    let redacted = kept
        .iter()
        .position(|row| {
            row["event"] == "send"
                && row["body"].is_null()
                && row["payload"].get("text_commitment").is_some()
        })
        .unwrap();
    kept[redacted]["body"] = "pasted by mistake".into();
    broken(&home.verify(&archive(&kept), &[]), 32, "body_mismatch");
}

#[test]
fn chain_breaks_name_the_oracle_reasons() {
    let home = Home::new();
    let all = rows(RICH);
    let mut edited = all.clone();
    edited[3]["payload"]["fields"]["status"] = "rewritten".into();
    broken(&home.verify(&archive(&edited), &[]), 4, "hash_mismatch");
    // The walk stops at the break, before a later unreadable line.
    let mut late = archive(&edited);
    late.extend(b"not JSON\n");
    broken(&home.verify(&late, &[]), 4, "hash_mismatch");
    let mut gap = all.clone();
    gap.remove(5);
    broken(&home.verify(&archive(&gap), &[]), 7, "sequence_gap");
    let mut link = all.clone();
    link[6]["prev_hash"] = "f".repeat(64).into();
    broken(&home.verify(&archive(&link), &[]), 7, "broken_link");
    let mut genesis = all.clone();
    genesis[0]["prev_hash"] = "1".repeat(64).into();
    broken(&home.verify(&archive(&genesis), &[]), 1, "broken_link");
    broken(
        &home.verify(&archive(&all[4..]), &[]),
        5,
        "unanchored_start",
    );
}

#[test]
fn unreadable_rows_are_named_by_python_line_number() {
    let home = Home::new();
    let all = rows(RICH);
    let target = send_index(&all, Some("redact me later"));
    let path = home.0.join("archive.jsonl");
    let named = |output: Output, line: usize, what: &str| {
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let expected = format!("board-audit: {} line {line} {what}\n", path.display());
        assert_eq!(output.stderr, cli::native_text(&expected));
    };
    let mut wrong = all.clone();
    wrong[target]["body"] = 7.into();
    named(
        home.verify(&archive(&wrong), &[]),
        39,
        "is not an exported audit event",
    );
    let mut text = b"\r\r \r\n".to_vec();
    let mut salted = all.clone();
    salted[target]["body_salt"] = 7.into();
    text.extend(
        archive(&salted)
            .iter()
            .map(|b| if *b == b'\n' { b'\r' } else { *b }),
    );
    named(
        home.verify(&text, &[]),
        42,
        "is not an exported audit event",
    );
    let mut seq = all.clone();
    seq[3]["seq"] = "4".into();
    named(
        home.verify(&archive(&seq), &[]),
        4,
        "is not an exported audit event",
    );
}

#[test]
fn a_missing_archive_is_named_and_nothing_is_created() {
    let home = Home::new();
    let missing = home.0.join("missing.jsonl");
    let output = command(&home.0)
        .args(["verify", "--input"])
        .arg(&missing)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        cli::native_text(&format!("board-audit: cannot read {}\n", missing.display()))
    );
    assert_eq!(fs::read_dir(&home.0).unwrap().count(), 0);
}

#[test]
fn an_existing_export_target_is_refused_before_any_bank() {
    let home = Home::new();
    let target = home.0.join("out.jsonl");
    fs::write(&target, b"keep").unwrap();
    let output = command(&home.0)
        .args(["export", "--out"])
        .arg(&target)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        cli::native_text(&format!(
            "board-audit: {} exists; export never replaces a file\n",
            target.display()
        ))
    );
    assert_eq!(fs::read(&target).unwrap(), b"keep");
}

#[test]
fn bank_actions_without_an_explicit_database_url_defer() {
    let home = Home::new();
    let deferral = cli::native_text(
        "board-audit: this path is deferred; native board-audit covers canonical verify, export and redact\n",
    );
    for args in [
        vec!["verify"],
        vec!["export", "--task", "t"],
        vec![
            "redact",
            "--message-id",
            "0123456789abcdef0123456789abcdef",
            "--reason",
            "why",
        ],
        vec!["stats"],
        vec!["export", "--since", "2026-09-23"],
        vec!["export", "--since", "1e9"],
        vec!["export", "--task=t"],
        vec!["redact", "--reason", "why"],
        vec!["verify", "--help"],
    ] {
        for url in [
            None,
            Some(""),
            Some("postgresql://u@127.0.0.1:1/x?application_name=a"),
        ] {
            let mut command = command(&home.0);
            command.args(&args).env("PSEUDOLIFE_DAEMON_EXEC", "1");
            if let Some(url) = url {
                command.env("PSEUDOLIFE_MCP_DATABASE_URL", url);
            }
            let output = command.output().unwrap();
            assert_eq!(output.status.code(), Some(1), "{args:?}");
            assert!(output.stdout.is_empty());
            assert_eq!(output.stderr, deferral, "{args:?}");
        }
    }
    assert_eq!(fs::read_dir(&home.0).unwrap().count(), 0);
}
