//
// Direct HealthKit Exporter — macOS 26
//

import HealthKit
import Foundation

let healthStore = HKHealthStore()

// Define identifiers
let sleepType = HKObjectType.categoryType(forIdentifier: .sleepAnalysis)
let hrvType = HKObjectType.quantityType(forIdentifier: .heartRateVariabilitySDNN)
let restingHRType = HKObjectType.quantityType(forIdentifier: .restingHeartRate)
let stepCountType = HKObjectType.quantityType(forIdentifier: .stepCount)

// Collect once
func collectSleep() {
    let startDate = Calendar.current.startOfDay(for: Date().addingTimeInterval(-24*3600))
    let endDate = Date()
    let predicate = HKQuery.predicateForSamples(withStart: startDate, end: endDate, options: .strictStartDate)
    let query = HKSampleQuery(sampleType: sleepType!, predicate: predicate, limit: 0, sortDescriptors: nil) { _, samples, _ in
        var hours = 0.0
        if let categorySamples = samples as? [HKCategorySample] {
            for sample in categorySamples {
                if sample.value == HKCategoryValueSleepAnalysis.inBed.rawValue {
                    let duration = sample.endDate.timeIntervalSince(sample.startDate)
                    hours += duration / 3600
                }
            }
        }
        result["sleep_hours"] = hours
        markComplete()
    }
    healthStore.execute(query)
}

func collectHRV() {
    let yesterday = Calendar.current.startOfDay(for: Date().addingTimeInterval(-24*3600))
    let dayAfter = Calendar.current.date(byAdding: .day, value: 1, to: yesterday)!
    let query = HKStatisticsQuery(quantityType: hrvType!, quantitySamplePredicate: nil, options: .discreteAverage) { _, stats, _ in
        if let avg = stats?.averageQuantity()?.doubleValue(for: HKUnit.second()) {
            result["hrv_ms"] = avg * 1000  // s → ms
        }
        markComplete()
    }
    healthStore.execute(query)
}

func collectRestingHR() {
    let yesterday = Calendar.current.startOfDay(for: Date().addingTimeInterval(-24*3600))
    let dayAfter = Calendar.current.date(byAdding: .day, value: 1, to: yesterday)!
    let query = HKStatisticsQuery(quantityType: restingHRType!, quantitySamplePredicate: nil, options: .discreteMin) { _, stats, _ in
        if let min = stats?.minimumQuantity()?.doubleValue(for: HKUnit.count().unitDivided(by: HKUnit.minute())) {
            result["resting_hr_bpm"] = min
        }
        markComplete()
    }
    healthStore.execute(query)
}

func collectStepCount() {
    let today = Calendar.current.startOfDay(for: Date())
    let predicate = HKQuery.predicateForSamples(withStart: today, end: Date(), options: .strictStartDate)
    let query = HKStatisticsQuery(quantityType: stepCountType!, quantitySamplePredicate: predicate, options: .cumulativeSum) { _, stats, _ in
        if let sum = stats?.sumQuantity()?.doubleValue(for: HKUnit.count()) {
            result["step_count"] = sum
        }
        markComplete()
    }
    healthStore.execute(query)
}

// Output
var result: [String: Any] = [:]
let expected = 4
var completed = 0

func markComplete() {
    completed += 1
    if completed >= expected {
        result["collected_at"] = ISO8601DateFormatter().string(from: Date())
        let outputDir = URL(fileURLWithPath: FileManager.default.homeDirectoryForCurrentUser.path)
            .appendingPathComponent(".openclaw/private/health")
        try? FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true, attributes: nil)
        let output = outputDir.appendingPathComponent("latest.json")
        let data = try! JSONSerialization.data(withJSONObject: result, options: .prettyPrinted)
        try? data.write(to: output)
        _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.5))
        exit(0)
    }
}

// Init
Task {
    let allTypes = Set([sleepType, hrvType, restingHRType, stepCountType].compactMap { $0 })
    do {
        let _ = try await healthStore.requestAuthorization(toShare: [], read: allTypes)
    } catch {
        print("Authorization failed: \(error)")
        exit(1)
    }
    collectSleep()
    collectHRV()
    collectRestingHR()
    collectStepCount()
}

_ = RunLoop.main
RunLoop.main.run()