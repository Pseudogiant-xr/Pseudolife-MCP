use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::oneshot;

pub struct Fixture {
    pub dsn: String,
    pub task: tokio::task::JoinHandle<()>,
}

async fn reply(stream: &mut tokio::net::TcpStream, tag: u8, data: &[u8]) {
    stream.write_u8(tag).await.unwrap();
    stream.write_u32(data.len() as u32 + 4).await.unwrap();
    stream.write_all(data).await.unwrap();
}

fn boolean_column() -> Vec<u8> {
    let mut row = Vec::from(1u16.to_be_bytes());
    row.extend_from_slice(b"exists\0");
    row.extend_from_slice(&0u32.to_be_bytes());
    row.extend_from_slice(&0u16.to_be_bytes());
    row.extend_from_slice(&16u32.to_be_bytes()); // PostgreSQL bool OID
    row.extend_from_slice(&1u16.to_be_bytes());
    row.extend_from_slice(&(-1i32).to_be_bytes());
    row.extend_from_slice(&0u16.to_be_bytes());
    row
}

pub async fn start() -> Fixture {
    start_inner(None).await
}

pub async fn fail_after_output_created() -> (Fixture, oneshot::Receiver<()>, oneshot::Sender<()>) {
    let (seen_tx, seen_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel();
    (
        start_inner(Some((seen_tx, release_rx))).await,
        seen_rx,
        release_tx,
    )
}

async fn start_inner(gate: Option<(oneshot::Sender<()>, oneshot::Receiver<()>)>) -> Fixture {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let length = stream.read_u32().await.unwrap();
        assert!((8..=65536).contains(&length));
        let mut startup = vec![0; length as usize - 4];
        stream.read_exact(&mut startup).await.unwrap();
        assert!(startup.starts_with(&[0, 3, 0, 0]));
        reply(&mut stream, b'R', &[0, 0, 0, 0]).await;
        reply(&mut stream, b'S', b"client_encoding\0UTF8\0").await;
        reply(&mut stream, b'Z', b"I").await;
        let mut transaction = b'I';
        let mut gate = gate;
        let mut failed = false;
        loop {
            let Ok(tag) = stream.read_u8().await else {
                break;
            };
            let length = stream.read_u32().await.unwrap();
            assert!((4..=65536).contains(&length));
            let mut data = vec![0; length as usize - 4];
            stream.read_exact(&mut data).await.unwrap();
            match tag {
                b'Q' => {
                    let query = std::str::from_utf8(&data[..data.len() - 1]).unwrap();
                    let command = query.split_whitespace().next().unwrap();
                    transaction = match command {
                        "BEGIN" => b'T',
                        "ROLLBACK" | "COMMIT" => b'I',
                        _ => transaction,
                    };
                    reply(&mut stream, b'C', format!("{command}\0").as_bytes()).await;
                    reply(&mut stream, b'Z', &[transaction]).await;
                }
                b'P' => {
                    let end = data.iter().position(|v| *v == 0).unwrap();
                    let query = &data[end + 1..];
                    if query
                        .windows(b"pg_attribute".len())
                        .any(|v| v == b"pg_attribute")
                        && let Some((seen, release)) = gate.take()
                    {
                        seen.send(()).unwrap();
                        release.await.unwrap();
                        reply(
                            &mut stream,
                            b'E',
                            b"SERROR\0VERROR\0CXX000\0Mfixture read failed\0\0",
                        )
                        .await;
                        failed = true;
                    } else {
                        reply(&mut stream, b'1', &[]).await;
                    }
                }
                b'D' if !failed => {
                    if data[0] == b'S' {
                        reply(&mut stream, b't', &[0, 0]).await;
                    }
                    reply(&mut stream, b'T', &boolean_column()).await;
                }
                b'B' if !failed => reply(&mut stream, b'2', &[]).await,
                b'E' if !failed => {
                    reply(&mut stream, b'D', &[0, 1, 0, 0, 0, 1, 1]).await;
                    reply(&mut stream, b'C', b"SELECT 1\0").await;
                }
                b'S' => {
                    reply(&mut stream, b'Z', &[transaction]).await;
                    failed = false;
                }
                b'C' => reply(&mut stream, b'3', &[]).await,
                b'X' => break,
                b'H' | b'D' | b'B' | b'E' => {}
                _ => panic!("unexpected frontend message {tag}"),
            }
        }
    });
    Fixture {
        dsn: format!("host=127.0.0.1 port={port} user=fixture dbname=fixture sslmode=disable"),
        task,
    }
}
