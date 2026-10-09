//! Pooling outside the graph, following SentenceTransformers' model modules.

use anyhow::{Result, bail};

#[derive(Clone, Copy, Debug)]
pub enum Pooling {
    Mean,
    Last,
}

pub fn pool(hidden: &[f32], mask: &[u32], dim: usize, mode: Pooling) -> Result<Vec<f32>> {
    if dim == 0 || hidden.len() != mask.len() * dim || mask.is_empty() {
        bail!("invalid hidden-state or attention-mask shape");
    }
    match mode {
        Pooling::Last => {
            let i = mask.iter().rposition(|m| *m != 0).unwrap_or(0);
            Ok(hidden[i * dim..(i + 1) * dim]
                .iter()
                .map(|v| v * mask[i] as f32)
                .collect())
        }
        Pooling::Mean => {
            let mut out = vec![0.; dim];
            for (token, m) in hidden.chunks_exact(dim).zip(mask) {
                for (sum, value) in out.iter_mut().zip(token) {
                    *sum += *value * *m as f32;
                }
            }
            let count = (mask.iter().sum::<u32>() as f32).max(1e-9);
            out.iter_mut().for_each(|v| *v /= count);
            Ok(out)
        }
    }
}

pub fn normalize(row: &mut [f32]) {
    let norm = row.iter().map(|v| v * v).sum::<f32>().sqrt().max(1e-12);
    for v in row {
        *v /= norm;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn masked_mean_and_last_token_follow_attention_mask() {
        let hidden = [1., 2., 3., 4., 99., 99.];
        assert_eq!(
            pool(&hidden, &[1, 1, 0], 2, Pooling::Mean).unwrap(),
            [2., 3.]
        );
        assert_eq!(
            pool(&hidden, &[1, 1, 0], 2, Pooling::Last).unwrap(),
            [3., 4.]
        );
        assert_eq!(
            pool(&hidden, &[0, 1, 1], 2, Pooling::Last).unwrap(),
            [99., 99.]
        );
        assert!(pool(&hidden, &[1, 1], 2, Pooling::Mean).is_err());
    }
}
