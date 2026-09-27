use chrono::{DateTime, Duration, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::America::Chicago;

/// Topstep trade dates roll at 17:00 in Chicago, including daylight-saving changes.
pub fn topstep_trade_date(timestamp: DateTime<Utc>) -> NaiveDate {
    let local = timestamp.with_timezone(&Chicago);
    let mut date = local.date_naive();
    if local.hour() >= 17 {
        date += Duration::days(1);
    }
    date
}

/// UTC bounds for a Topstep trade date whose label is `trade_date`.
pub fn topstep_trade_date_bounds(trade_date: NaiveDate) -> (DateTime<Utc>, DateTime<Utc>) {
    let start_day = trade_date - Duration::days(1);
    let at_five = NaiveTime::from_hms_opt(17, 0, 0).expect("17:00 is a valid time");
    let start_local = Chicago
        .from_local_datetime(&start_day.and_time(at_five))
        .single()
        .expect("17:00 Chicago is unambiguous");
    let end_local = Chicago
        .from_local_datetime(&trade_date.and_time(at_five))
        .single()
        .expect("17:00 Chicago is unambiguous");
    (
        start_local.with_timezone(&Utc),
        end_local.with_timezone(&Utc),
    )
}

use chrono::Timelike;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trade_date_changes_at_five_chicago_across_dst() {
        let before = DateTime::parse_from_rfc3339("2026-03-08T21:59:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let at_roll = DateTime::parse_from_rfc3339("2026-03-08T22:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(topstep_trade_date(before).to_string(), "2026-03-08");
        assert_eq!(topstep_trade_date(at_roll).to_string(), "2026-03-09");

        let bounds = topstep_trade_date_bounds(NaiveDate::from_ymd_opt(2026, 3, 9).unwrap());
        assert_eq!(bounds.0.to_rfc3339(), "2026-03-08T22:00:00+00:00");
        assert_eq!(bounds.1.to_rfc3339(), "2026-03-09T22:00:00+00:00");
    }
}
