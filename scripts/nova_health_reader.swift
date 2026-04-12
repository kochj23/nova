// nova_health_reader.swift — Read Apple Health data via HealthKit
//
// Compiled binary reads HealthKit and outputs JSON to stdout.
// Must be compiled and signed with HealthKit entitlement:
//
//   swiftc nova_health_reader.swift -o nova_health_reader -framework HealthKit
//   codesign --sign "Apple Development" --entitlements nova_health_reader.entitlements nova_health_reader
//
// First run will prompt for Health access permission.
//
// Usage:
//   ./nova_health_reader                    # Last 24h of all types
//   ./nova_health_reader --hours 48         # Last 48 hours
//   ./nova_health_reader --type heart_rate  # Specific type only
//   ./nova_health_reader --types            # List available types
//
// Written by Jordan Koch.

import Foundation
import HealthKit

let store = HKHealthStore()

// ── Supported data types ────────────────────────────────────────────────────

struct HealthType {
    let key: String
    let identifier: HKQuantityTypeIdentifier
    let unit: HKUnit
    let unitLabel: String
}

let healthTypes: [HealthType] = [
    HealthType(key: "heart_rate",           identifier: .heartRate,                    unit: HKUnit.count().unitDivided(by: .minute()), unitLabel: "bpm"),
    HealthType(key: "blood_pressure_sys",   identifier: .bloodPressureSystolic,        unit: .millimeterOfMercury(),                    unitLabel: "mmHg"),
    HealthType(key: "blood_pressure_dia",   identifier: .bloodPressureDiastolic,       unit: .millimeterOfMercury(),                    unitLabel: "mmHg"),
    HealthType(key: "blood_oxygen",         identifier: .oxygenSaturation,             unit: .percent(),                                unitLabel: "%"),
    HealthType(key: "body_temperature",     identifier: .bodyTemperature,              unit: .degreeFahrenheit(),                       unitLabel: "°F"),
    HealthType(key: "respiratory_rate",     identifier: .respiratoryRate,              unit: HKUnit.count().unitDivided(by: .minute()), unitLabel: "breaths/min"),
    HealthType(key: "weight",              identifier: .bodyMass,                     unit: .pound(),                                  unitLabel: "lbs"),
    HealthType(key: "body_fat",            identifier: .bodyFatPercentage,            unit: .percent(),                                unitLabel: "%"),
    HealthType(key: "bmi",                 identifier: .bodyMassIndex,                unit: .count(),                                  unitLabel: ""),
    HealthType(key: "steps",               identifier: .stepCount,                    unit: .count(),                                  unitLabel: "steps"),
    HealthType(key: "distance",            identifier: .distanceWalkingRunning,       unit: .mile(),                                   unitLabel: "mi"),
    HealthType(key: "active_energy",       identifier: .activeEnergyBurned,           unit: .kilocalorie(),                            unitLabel: "kcal"),
    HealthType(key: "resting_energy",      identifier: .basalEnergyBurned,            unit: .kilocalorie(),                            unitLabel: "kcal"),
    HealthType(key: "blood_glucose",       identifier: .bloodGlucose,                 unit: HKUnit.gramUnit(with: .milli).unitDivided(by: .liter()), unitLabel: "mg/dL"),
    HealthType(key: "resting_heart_rate",  identifier: .restingHeartRate,             unit: HKUnit.count().unitDivided(by: .minute()), unitLabel: "bpm"),
    HealthType(key: "walking_heart_rate",  identifier: .walkingHeartRateAverage,      unit: HKUnit.count().unitDivided(by: .minute()), unitLabel: "bpm"),
    HealthType(key: "hrv",                 identifier: .heartRateVariabilitySDNN,     unit: .secondUnit(with: .milli),                 unitLabel: "ms"),
    HealthType(key: "vo2max",              identifier: .vo2Max,                       unit: HKUnit.literUnit(with: .milli).unitDivided(by: HKUnit.gramUnit(with: .kilo).unitMultiplied(by: .minute())), unitLabel: "mL/kg/min"),
]

// ── Arguments ───────────────────────────────────────────────────────────────

var hoursBack: Double = 24
var filterType: String? = nil
var listTypes = false

var args = CommandLine.arguments.dropFirst()
while let arg = args.first {
    args = args.dropFirst()
    switch arg {
    case "--hours":
        if let next = args.first, let h = Double(next) {
            hoursBack = h
            args = args.dropFirst()
        }
    case "--type":
        if let next = args.first {
            filterType = next
            args = args.dropFirst()
        }
    case "--types":
        listTypes = true
    default:
        break
    }
}

if listTypes {
    for t in healthTypes {
        print("  \(t.key) (\(t.unitLabel))")
    }
    exit(0)
}

// ── HealthKit authorization ─────────────────────────────────────────────────

guard HKHealthStore.isHealthDataAvailable() else {
    let err: [String: Any] = ["error": "Health data not available on this device"]
    if let data = try? JSONSerialization.data(withJSONObject: err),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
    exit(1)
}

let readTypes: Set<HKObjectType> = Set(healthTypes.compactMap {
    HKQuantityType.quantityType(forIdentifier: $0.identifier)
})

// Add sleep analysis (category type, not quantity)
let sleepType = HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!
var allReadTypes = readTypes
allReadTypes.insert(sleepType)

let sem = DispatchSemaphore(value: 0)
var authError: Error? = nil

store.requestAuthorization(toShare: nil, read: allReadTypes) { success, error in
    if !success { authError = error }
    sem.signal()
}
_ = sem.wait(timeout: .now() + 10)

if let err = authError {
    let errDict: [String: Any] = ["error": "Authorization failed: \(err.localizedDescription)"]
    if let data = try? JSONSerialization.data(withJSONObject: errDict),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
    exit(1)
}

// ── Query ───────────────────────────────────────────────────────────────────

let startDate = Date().addingTimeInterval(-hoursBack * 3600)
let endDate = Date()
let predicate = HKQuery.predicateForSamples(withStart: startDate, end: endDate)

let fmt = DateFormatter()
fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
fmt.timeZone = TimeZone.current

var results: [String: [[String: Any]]] = [:]
let typesToQuery = filterType != nil
    ? healthTypes.filter { $0.key == filterType }
    : healthTypes

let group = DispatchGroup()

for ht in typesToQuery {
    guard let quantityType = HKQuantityType.quantityType(forIdentifier: ht.identifier) else { continue }

    group.enter()
    let query = HKSampleQuery(
        sampleType: quantityType,
        predicate: predicate,
        limit: HKObjectQueryNoLimit,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]
    ) { _, samples, error in
        defer { group.leave() }
        guard let samples = samples as? [HKQuantitySample], !samples.isEmpty else { return }

        var entries: [[String: Any]] = []
        for sample in samples {
            let value = sample.quantity.doubleValue(for: ht.unit)
            entries.append([
                "value": round(value * 100) / 100,
                "unit": ht.unitLabel,
                "date": fmt.string(from: sample.startDate),
                "source": sample.sourceRevision.source.name,
            ])
        }
        DispatchQueue.main.sync {
            results[ht.key] = entries
        }
    }
    store.execute(query)
}

// Sleep analysis (category type — separate query)
if filterType == nil || filterType == "sleep" {
    group.enter()
    let sleepQuery = HKSampleQuery(
        sampleType: sleepType,
        predicate: predicate,
        limit: HKObjectQueryNoLimit,
        sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]
    ) { _, samples, error in
        defer { group.leave() }
        guard let samples = samples as? [HKCategorySample], !samples.isEmpty else { return }

        var entries: [[String: Any]] = []
        for sample in samples {
            let stage: String
            switch sample.value {
            case HKCategoryValueSleepAnalysis.inBed.rawValue: stage = "in_bed"
            case HKCategoryValueSleepAnalysis.asleepCore.rawValue: stage = "core"
            case HKCategoryValueSleepAnalysis.asleepDeep.rawValue: stage = "deep"
            case HKCategoryValueSleepAnalysis.asleepREM.rawValue: stage = "rem"
            case HKCategoryValueSleepAnalysis.awake.rawValue: stage = "awake"
            default: stage = "unknown"
            }
            let durationMin = sample.endDate.timeIntervalSince(sample.startDate) / 60
            entries.append([
                "stage": stage,
                "duration_min": round(durationMin * 10) / 10,
                "start": fmt.string(from: sample.startDate),
                "end": fmt.string(from: sample.endDate),
                "source": sample.sourceRevision.source.name,
            ])
        }
        DispatchQueue.main.sync {
            results["sleep"] = entries
        }
    }
    store.execute(sleepQuery)
}

_ = group.wait(timeout: .now() + 30)

// ── Output ──────────────────────────────────────────────────────────────────

let output: [String: Any] = [
    "period_hours": hoursBack,
    "start": fmt.string(from: startDate),
    "end": fmt.string(from: endDate),
    "types_queried": typesToQuery.map { $0.key } + (filterType == nil || filterType == "sleep" ? ["sleep"] : []),
    "readings": results,
]

if let data = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]),
   let str = String(data: data, encoding: .utf8) {
    print(str)
} else {
    print("{\"error\": \"JSON serialization failed\"}")
    exit(1)
}
