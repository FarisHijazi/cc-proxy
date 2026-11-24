//! What a request on a Console API key may cost, from the official prices in
//! `scripts/anthropic_prices.json` (the same file the proxy's spend tracking reads).

use crate::anthropic::schema::MessagesRequest;
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::OnceLock;

/// Claude 4.7 and later count about 30% more tokens than the generic estimator.
const TOKENIZER_MARGIN: f64 = 1.3;
/// Output is billed as produced; most turns stop far below `max_tokens`.
const OUTPUT_ALLOWANCE: u32 = 8192;
const LONG_PROMPT: f64 = 100_000.0;

#[derive(Deserialize)]
struct Rates {
    cache_write_1h: f64,
    output: f64,
    over_100k: Option<Box<Rates>>,
}

#[derive(Deserialize)]
struct Prices {
    models: HashMap<String, Rates>,
}

fn prices() -> &'static HashMap<String, Rates> {
    static PRICES: OnceLock<HashMap<String, Rates>> = OnceLock::new();
    PRICES.get_or_init(|| {
        serde_json::from_str::<Prices>(include_str!("../scripts/anthropic_prices.json"))
            .map(|prices| prices.models)
            .unwrap_or_default()
    })
}

/// A price id, or that id with a `-YYYYMMDD` snapshot suffix.
fn rates(model: &str) -> Option<&'static Rates> {
    let prices = prices();
    prices.get(model).or_else(|| {
        let (base, date) = model.rsplit_once('-')?;
        (date.len() == 8 && date.bytes().all(|b| b.is_ascii_digit()))
            .then(|| prices.get(base))
            .flatten()
    })
}

/// USD the request could cost at most, as if its whole prompt were written to the 1-hour cache:
/// a session moved onto the key rewrites its cache there. None for a model without a known price.
pub fn estimate_usd(model: &str, body: &MessagesRequest) -> Option<f64> {
    let prompt = crate::providers::kimi::count_tokens::count_tokens(body) as f64 * TOKENIZER_MARGIN;
    let base = rates(model)?;
    let rates = match &base.over_100k {
        Some(long) if prompt > LONG_PROMPT => long,
        _ => base,
    };
    let output = body
        .max_tokens
        .unwrap_or(OUTPUT_ALLOWANCE)
        .min(OUTPUT_ALLOWANCE) as f64;
    Some((prompt * rates.cache_write_1h + output * rates.output) / 1e6)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request(text: &str, max_tokens: u32) -> MessagesRequest {
        serde_json::from_value(json!({"model": "m", "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": text}]}))
        .unwrap()
    }

    #[test]
    fn every_listed_price_parses_and_snapshot_ids_resolve() {
        assert!(prices().len() >= 18, "the price file failed to parse");
        assert!(rates("claude-haiku-4-5-20251001").is_some());
        assert!(rates("claude-opus-5-55").is_none());
        assert!(rates("claude-opus-5-5-x").is_none());
    }

    #[test]
    fn the_estimate_covers_a_full_cache_write_plus_the_output_allowance() {
        let small = estimate_usd("claude-opus-5-5", &request("hi", 100_000)).unwrap();
        let output_only = OUTPUT_ALLOWANCE as f64 * 20.0 / 1e6;
        assert!(small >= output_only && small < output_only + 0.001);
        let long = "word ".repeat(400_000);
        let haiku = estimate_usd("claude-haiku-5-5", &request(&long, 1)).unwrap();
        assert!(
            haiku > 100_000.0 * 1.0 / 1e6,
            "a long Haiku 5.5 prompt uses its higher price"
        );
        assert!(estimate_usd("claude-unknown-9", &request("hi", 1)).is_none());
    }
}
