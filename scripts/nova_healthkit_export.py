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

let healthStore = HKHealthStore()

// Define types to read
guard let sleepType = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { exit(1) }
guard let hrvType = HKObjectType.quantityType(forIdentifier: .heartRateVariabilitySDNN) else { exit(1) }
guard let hrType = HKObjectType.quantityType(forIdentifier: .heartRate) else { exit(1) }
guard let stepType = HKObjectType.quantityType(forIdentifier: .stepCount) else { exit(1) }

do {
    try healthStore.requestAuthorization(toShare: [], read: [sleepType, hrvType, hrType, stepType])
} catch {
    print("Auth failed: \(error)")
    exit(1)
}

class DataCollector: NSObject {
    var results = [String: Any]()
    let group = DispatchGroup()
    
    // Get sleep from last 24h
    func fetchSleep() {
        group.enter()
        let start = Calendar.current.startOfDay(for: Date().addingTimeInterval(-24*3600))
        let predicate = HKQuery.predicateForSamples(withStart: start, end: Date(), options: .strictStartDate)
        let query = HKSampleQuery(sampleType: sleepType!, predicate: predicate, limit: 0, sortDescriptors: nil) { _, samples, _ in
            var sleepHours = 0.0
            if let samples = samples as? [HKCategorySample] {
                for sample in samples {
                    if sample.value == HKCategoryValueSleepAnalysis.inBed.rawValue {
                        let duration = sample.endDate.timeIntervalSince(sample.startDate)
                        sleepHours += duration / 3600.0
                    }
                }
            }
            self.results["sleep_hours"] = sleepHours
            self.group.leave()
        }
        healthStore.execute(query)
    }
    
    // Get HRV (SDNN) from yesterday
    func fetchHRV() {
        group.enter()
        var interval = DateInterval()
        Calendar.current.dateInterval(of: .day, start: &interval.start, interval: nil, for: Date().addingTimeInterval(-24*3600))
        let query = HKStatisticsCollectionQuery(quantityType: hrvType!, quantitySamplePredicate: nil, options: .discreteAverage, anchorDate: interval.start, intervalComponents: DateComponents(day: 1))
        query.initialResultsHandler = { _, statsColl, _ in
            if let stats = statsColl?.statistics(for: interval) {
                let avg = stats.last?.averageQuantity()?.doubleValue(for: HKUnit.millisecond())
                self.results["hrv_sdnn_ms"] = avg
            }
            self.group.leave()
        }
        healthStore.execute(query)
    }

    // Get resting heart rate from yesterday
    func fetchRestingHR() {
        group.enter()
        var interval = DateInterval()
        Calendar.current.dateInterval(of: .day, start: &interval.start, interval: nil, for: Date().addingTimeInterval(-24*3600))
        let query = HKStatisticsCollectionQuery(quantityType: hrType!, quantitySamplePredicate: nil, options: .discreteMinimum, anchorDate: interval.start, intervalComponents: DateComponents(day: 1))
        query.initialResultsHandler = { _, statsColl, _ in
            if let stats = statsColl?.statistics(for: interval) {
                let minHR = stats.last?.minimumQuantity()?.doubleValue(for: HKUnit.count().unitDivided(by: HKUnit.minute()))
                self.results["resting_heart_rate_bpm"] = minHR
            }
            self.group.leave()
        }
        healthStore.execute(query)
    }

    // Get step count from today
    func fetchSteps() {
        group.enter()
        let start = Calendar.current.startOfDay(for: Date())
        let predicate = HKQuery.predicateForSamples(withStart: start, end: Date(), options: .strictStartDate)
        let query = HKStatisticsQuery(quantityType: stepType!, quantitySamplePredicate: predicate, options: .cumulativeSum) { _, stats, _ in
            let steps = stats?.sumQuantity()?.doubleValue(for: HKUnit.count())
            self.results["step_count"] = steps
            self.group.leave()
        }
        healthStore.execute(query)
    }
    
    func collect(completion: @escaping ([String: Any]) -> Void) {
        fetchSleep()
        fetchHRV()
        fetchRestingHR()
        fetchSteps()
        
        group.notify(queue: .main) {
            completion(self.results)
        }
    }
}

let collector = DataCollector()
collector.collect { results in
    print("COLLECTED: \(results)")
    exit(0)
}

// Block
let sem = DispatchSemaphore(value: 0)
sem.wait()
'''

# ── Main Execution ──────────────────────────────────────────────────────────────

def main():
    # Write Swift script to temp file
    swift_path = Path.home() / f"Library/Containers/com.digitalnoise.healthkitexport/Data/swift_script_{uuid.uuid4().hex}.swift"
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
