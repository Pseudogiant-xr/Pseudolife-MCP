//! Classify an owned daemon's diagnostics without exposing its raw stderr.
use std::sync::{
    Arc,
    atomic::{AtomicU8, Ordering},
};
use tokio::io::{AsyncRead, AsyncReadExt};

#[derive(Default)]
pub(super) struct Diagnostics {
    code: Arc<AtomicU8>,
    reader: Option<tokio::task::JoinHandle<()>>,
}

fn classify(bytes: &[u8]) -> u8 {
    let text = String::from_utf8_lossy(bytes);
    if text.starts_with("Cross-encoder reranker failed to load (") {
        return 0;
    }
    if text.contains("model checksum mismatch")
        || text.contains("missing or unsafe model metadata")
        || text.contains("model artifact is missing or unsafe:")
        || text.contains("Qwen model artifact is missing or incomplete in ")
        || (text.contains("configured ONNX artifact ")
            && text.contains("is not available in the local model or Hub cache;"))
    {
        1
    } else if text.contains("load the ONNX Runtime")
        || text.contains("loading ONNX Runtime")
        || text.contains("onnxruntime.dll")
        || text.contains("libonnxruntime.so")
        || text.contains("libonnxruntime.dylib")
    {
        2
    } else if text.contains("Address already in use") || text.contains("address already in use") {
        3
    } else {
        0
    }
}

impl Diagnostics {
    pub(super) fn watch(&mut self, mut input: impl AsyncRead + Unpin + Send + 'static) {
        let code = self.code.clone();
        self.reader = Some(tokio::spawn(async move {
            let mut line = Vec::new();
            let mut buffer = [0; 4096];
            while let Ok(count) = input.read(&mut buffer).await {
                if count == 0 {
                    break;
                }
                for &byte in &buffer[..count] {
                    if byte == b'\n' {
                        let observed = classify(&line);
                        if observed != 0 {
                            code.store(observed, Ordering::Release);
                        }
                        line.clear();
                    } else if line.len() < 8192 {
                        // Retain the prefix, so a long optional-reranker
                        // warning cannot lose its scope across read chunks.
                        line.push(byte);
                    }
                }
            }
            let observed = classify(&line);
            if observed != 0 {
                code.store(observed, Ordering::Release);
            }
            // Neither raw text nor paths, credentials or RPC bodies leave this task.
        }));
    }
    pub(super) fn error(&self) -> Option<&'static str> {
        match self.code.load(Ordering::Acquire) {
            1 => Some("ProvisionedModelIntegrityError"),
            2 => Some("OnnxRuntimeUnavailable"),
            3 => Some("FixtureListenerUnavailable"),
            _ => None,
        }
    }
    pub(super) async fn close(&mut self) {
        if let Some(reader) = self.reader.take() {
            let _ = reader.await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn optional_reranker_warning_never_becomes_an_embedding_failure() {
        use tokio::io::AsyncWriteExt;
        let (mut write, read) = tokio::io::duplex(64);
        let mut diagnostics = Diagnostics::default();
        diagnostics.watch(read);
        write
            .write_all(
                b"Cross-encoder reranker failed to load (model artifact is missing or unsafe:",
            )
            .await
            .unwrap();
        write.write_all(&vec![b'x'; 9000]).await.unwrap();
        write
            .write_all(b"model checksum mismatch) - disabling reranking\n")
            .await
            .unwrap();
        drop(write);
        diagnostics.close().await;
        assert_eq!(diagnostics.error(), None);
    }
    #[tokio::test]
    async fn diagnostics_cross_chunks_and_never_return_untrusted_text() {
        use tokio::io::AsyncWriteExt;
        let (mut write, read) = tokio::io::duplex(64);
        let mut diagnostics = Diagnostics::default();
        diagnostics.watch(read);
        write
            .write_all(b"private password=value: model checksum ")
            .await
            .unwrap();
        write.write_all(b"mismatch for private/path").await.unwrap();
        drop(write);
        diagnostics.close().await;
        assert_eq!(diagnostics.error(), Some("ProvisionedModelIntegrityError"));
        assert_eq!(
            classify(b"could not load the ONNX Runtime at private/path"),
            2
        );
        assert_eq!(classify(b"Address already in use"), 3);
        assert_eq!(classify(b"configured ONNX artifact \"onnx/model.onnx\" for \"private/path\" is not available in the local model or Hub cache; run repair"), 1);
        assert_eq!(
            classify(b"model artifact is missing or unsafe: private/path"),
            1
        );
        assert_eq!(classify(b"unknown failure with password=value"), 0);
    }
}
