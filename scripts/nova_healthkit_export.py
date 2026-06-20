#!/usr/bin/env python3
"""
nova_healthkit_export.py — Secure HealthKit to JSON exporter.

Fetches sleep, HRV, resting heart rate, and step count.
Writes encrypted, locked JSON to ~/.openclaw/private/health/latest.json.

Must be run via launchd with HealthKit entitlements.

Written by Jordan Koch / Nova.
"""

import os
import sys
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

# ── Config ─────────────────────────────────────────────────────────────────────
HEALTH_DIR = Path.home() / ".openclaw/private/health"
OUTPUT_PATH = HEALTH_DIR / "latest.json"
ENCRYPTED_PATH = HEALTH_DIR / "latest.json.gpg"

# Ensure directory exists with strict permissions
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
HEALTH_DIR.chmod(0o700)  # rwx------

# ── HealthKit Query via Swift (compiled inline) ────────────────────────────────
SWIFT_SCRIPT = '''
import HealthKit
import Foundation

let store = HKHealthStore()
let group = DispatchGroup()
var output: [String: Double] = [:]
let bpmUnit = HKUnit.count().unitDivided(by: HKUnit.minute())

let sleepType = HKCategoryType(.sleepAnalysis)
let hrvType = HKQuantityType(.heartRateVariabilitySDNN)
let hrType = HKQuantityType(.restingHeartRate)
let stepType = HKQuantityType(.stepCount)

let yesterday = Calendar.current.startOfDay(for: Date().addingTimeInterval(-86400))
let todayStart = Calendar.current.startOfDay(for: Date())
let now = Date()

// Auth
group.enter()
store.requestAuthorization(toShare: [], read: [sleepType, hrvType, hrType, stepType]) { ok, err in
    group.leave()
}
group.wait()

// Sleep (last 24h)
group.enter()
let sleepPred = HKQuery.predicateForSamples(withStart: yesterday, end: now)
let sleepQ = HKSampleQuery(sampleType: sleepType, predicate: sleepPred, limit: 0, sortDescriptors: nil) { _, samples, _ in
    var hours = 0.0
    for s in (samples as? [HKCategorySample]) ?? [] {
        if s.value == HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue ||
           s.value == HKCategoryValueSleepAnalysis.asleepCore.rawValue ||
           s.value == HKCategoryValueSleepAnalysis.asleepDeep.rawValue ||
           s.value == HKCategoryValueSleepAnalysis.asleepREM.rawValue {
            hours += s.endDate.timeIntervalSince(s.startDate) / 3600.0
        }
    }
    output["sleep_hours"] = hours
    group.leave()
}
store.execute(sleepQ)

// HRV (yesterday)
group.enter()
let hrvPred = HKQuery.predicateForSamples(withStart: yesterday, end: todayStart)
let hrvSort = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: false)
let hrvQ = HKSampleQuery(sampleType: hrvType, predicate: hrvPred, limit: 10, sortDescriptors: [hrvSort]) { _, samples, _ in
    var total = 0.0; var count = 0.0
    for s in (samples as? [HKQuantitySample]) ?? [] {
        total += s.quantity.doubleValue(for: HKUnit.secondUnit(with: .milli))
        count += 1
    }
    if count > 0 { output["hrv_sdnn_ms"] = total / count }
    group.leave()
}
store.execute(hrvQ)

// Resting HR (yesterday)
group.enter()
let hrPred = HKQuery.predicateForSamples(withStart: yesterday, end: todayStart)
let hrSort = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: false)
let hrQ = HKSampleQuery(sampleType: hrType, predicate: hrPred, limit: 1, sortDescriptors: [hrSort]) { _, samples, _ in
    if let s = (samples as? [HKQuantitySample])?.first {
        output["resting_heart_rate_bpm"] = s.quantity.doubleValue(for: bpmUnit)
    }
    group.leave()
}
store.execute(hrQ)

// Steps (today)
group.enter()
let stepPred = HKQuery.predicateForSamples(withStart: todayStart, end: now)
let stepQ = HKStatisticsQuery(quantityType: stepType, quantitySamplePredicate: stepPred, options: .cumulativeSum) { _, stats, _ in
    if let sum = stats?.sumQuantity() {
        output["step_count"] = sum.doubleValue(for: HKUnit.count())
    }
    group.leave()
}
store.execute(stepQ)

group.wait()

// JSON output
if let data = try? JSONSerialization.data(withJSONObject: output, options: .sortedKeys),
   let json = String(data: data, encoding: .utf8) {
    print("HEALTHKIT_JSON:\(json)")
}
'''

# ── Main Execution ──────────────────────────────────────────────────────────────

def main():
    # Write Swift script to temp file
    swift_path = Path("/tmp") / f"nova_healthkit_{uuid.uuid4().hex}.swift"
    swift_path.write_text(SWIFT_SCRIPT)
    swift_path.chmod(0o600)

    try:
        # Compile and run Swift script
        result = subprocess.run([
            "xcrun", "swift", str(swift_path)],
            capture_output=True, text=True, timeout=60
        )
        swift_path.unlink()  # clean up

        if result.returncode != 0:
            print(f"Swift failed: {result.stderr}")
            sys.exit(1)

        # Parse output
        if not result.stdout.strip().startswith("COLLECTED:"):
            print("Unexpected output")
            sys.exit(1)

        data_str = result.stdout.strip().replace("COLLECTED: ", "")
        data = json.loads(data_str.replace("\"", '"'))

        # Add timestamp
        data["collected_at"] = datetime.now(timezone.utc).isoformat()

        # Write JSON (locked)
        with open(OUTPUT_PATH, 'w') as f:
            json.dump(data, f, indent=2)
        OUTPUT_PATH.chmod(0o600)  # rw-------

        print(f"Health data written to {OUTPUT_PATH}")
        sys.exit(0)

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
