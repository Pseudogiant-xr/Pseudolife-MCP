//! Disposable PostgreSQL wire faults; no database login or server is used.
use tokio::io::{AsyncReadExt, AsyncWriteExt};

pub const SECRET: &str = "synthetic-audit-driver-secret";

async fn reply(stream: &mut tokio::net::TcpStream, tag: u8, data: &[u8]) {
    stream.write_u8(tag).await.unwrap();
    stream.write_u32(data.len() as u32 + 4).await.unwrap();
    stream.write_all(data).await.unwrap();
}

pub async fn start(fault: &str) -> (String, tokio::task::JoinHandle<()>) {
    let fault = fault.to_owned();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let length = stream.read_u32().await.unwrap();
        let mut startup = vec![0; length as usize - 4];
        stream.read_exact(&mut startup).await.unwrap();
        reply(&mut stream, b'R', &[0, 0, 0, 0]).await;
        reply(&mut stream, b'S', b"client_encoding\0UTF8\0").await;
        reply(&mut stream, b'Z', b"I").await;
        let mut failed = false;
        let mut boolean = false;
        let mut parameters = 0u16;
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
                    if fault == "snapshot"
                        && (query.starts_with("BEGIN") || query.starts_with("START"))
                    {
                        reply(
                            &mut stream,
                            b'E',
                            format!("SERROR\0VERROR\0C42501\0M{SECRET}\0\0").as_bytes(),
                        )
                        .await;
                    } else {
                        let command = query.split_whitespace().next().unwrap();
                        reply(&mut stream, b'C', format!("{command}\0").as_bytes()).await;
                    }
                    reply(&mut stream, b'Z', b"I").await;
                }
                b'P' => {
                    let end = data.iter().position(|v| *v == 0).unwrap();
                    let query = std::str::from_utf8(&data[end + 1..]).unwrap();
                    let body = query.contains("pg_attribute");
                    let table = query.contains("to_regclass") && !body;
                    boolean = body || table || query.contains("pg_advisory_xact_lock");
                    parameters = query
                        .split('$')
                        .skip(1)
                        .filter_map(|part| {
                            part.chars()
                                .take_while(char::is_ascii_digit)
                                .collect::<String>()
                                .parse::<u16>()
                                .ok()
                        })
                        .max()
                        .unwrap_or(0);
                    if fault == "table" && table || fault == "body" && body {
                        reply(
                            &mut stream,
                            b'E',
                            format!("SERROR\0VERROR\0C42501\0M{SECRET}\0\0").as_bytes(),
                        )
                        .await;
                        failed = true;
                    } else {
                        reply(&mut stream, b'1', &[]).await;
                    }
                }
                b'D' if !failed => {
                    if data[0] == b'S' {
                        let mut types = parameters.to_be_bytes().to_vec();
                        for _ in 0..parameters {
                            types.extend_from_slice(&25u32.to_be_bytes());
                        }
                        reply(&mut stream, b't', &types).await;
                    }
                    let mut columns = vec![0, u8::from(boolean)];
                    if boolean {
                        columns.extend_from_slice(b"exists\0");
                        columns.extend_from_slice(&0u32.to_be_bytes());
                        columns.extend_from_slice(&0u16.to_be_bytes());
                        columns.extend_from_slice(&16u32.to_be_bytes());
                        columns.extend_from_slice(&1u16.to_be_bytes());
                        columns.extend_from_slice(&(-1i32).to_be_bytes());
                        columns.extend_from_slice(&0u16.to_be_bytes());
                    }
                    reply(&mut stream, b'T', &columns).await;
                }
                b'B' if !failed => reply(&mut stream, b'2', &[]).await,
                b'E' if !failed => {
                    if boolean {
                        reply(&mut stream, b'D', &[0, 1, 0, 0, 0, 1, 1]).await;
                    }
                    reply(
                        &mut stream,
                        b'C',
                        if boolean {
                            b"SELECT 1\0"
                        } else {
                            b"SELECT 0\0"
                        },
                    )
                    .await;
                }
                b'S' => {
                    reply(&mut stream, b'Z', b"I").await;
                    failed = false;
                }
                b'C' => reply(&mut stream, b'3', &[]).await,
                b'X' => break,
                b'H' | b'D' | b'B' | b'E' => {}
                _ => panic!("unexpected frontend tag {tag}"),
            }
        }
    });
    (
        format!("host=127.0.0.1 port={port} user=fixture dbname=fixture sslmode=disable"),
        task,
    )
}
