pub struct Stats { count i64, sum i64 }
pub fn add_sample(mut s Stats, v i64) { s.count += 1; s.sum += v; }
