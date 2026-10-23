//! Dream cycle contracts: the cursor engine, the extractor client and the
//! `memory_dream` actions served on `/mcp`.
#![allow(dead_code)]

pub mod cursors;
pub mod deep;
pub mod extractor;
pub mod gates;
pub mod judge;
pub mod judge_agreement;
pub mod lessons;
pub mod prompts;
pub mod py_json_error;
pub mod relations;
pub mod review_judgments;
pub mod run;
pub mod service;
pub mod stage_calls;
pub mod stages;
pub mod token;
