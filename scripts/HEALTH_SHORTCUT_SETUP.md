# Nova Health Shortcut — iPhone Setup

This Shortcut runs daily on your iPhone, reads HealthKit data, and saves
it to iCloud Drive where Nova picks it up on the Mac.

## Create the Shortcut

Open **Shortcuts** on your iPhone and create a new shortcut called **"Nova Health Export"**.

### Actions (in order):

1. **Find Health Samples** where
   - Type is **Heart Rate**
   - Start Date is in the last **1 day**
   - Sort by **Start Date** (newest first)
   - Limit to **100**
   → Set variable: `heartRate`

2. **Find Health Samples** where
   - Type is **Blood Pressure (Systolic)**
   - Start Date is in the last **1 day**
   → Set variable: `bpSys`

3. **Find Health Samples** where
   - Type is **Blood Pressure (Diastolic)**
   - Start Date is in the last **1 day**
   → Set variable: `bpDia`

4. **Find Health Samples** where
   - Type is **Blood Oxygen**
   - Start Date is in the last **1 day**
   → Set variable: `spo2`

5. **Find Health Samples** where
   - Type is **Weight**
   - Start Date is in the last **7 days**
   - Limit to **10**
   → Set variable: `weight`

6. **Find Health Samples** where
   - Type is **Blood Glucose**
   - Start Date is in the last **1 day**
   → Set variable: `glucose`

7. **Find Health Samples** where
   - Type is **Steps**
   - Start Date is in the last **1 day**
   → Set variable: `steps`

8. **Find Health Samples** where
   - Type is **Resting Heart Rate**
   - Start Date is in the last **1 day**
   → Set variable: `restingHR`

9. **Find Health Samples** where
   - Type is **Sleep Analysis**
   - Start Date is in the last **1 day**
   → Set variable: `sleep`

10. **Find Health Samples** where
    - Type is **Heart Rate Variability**
    - Start Date is in the last **1 day**
    → Set variable: `hrv`

11. **Find Health Samples** where
    - Type is **Active Energy**
    - Start Date is in the last **1 day**
    → Set variable: `activeEnergy`

12. **Text** action — paste this template:
```
{"date":"[Current Date, format: yyyy-MM-dd]","readings":{"heart_rate":[Repeat with each item in heartRate: {"value":[Value],"unit":"bpm","date":"[Start Date, format: yyyy-MM-dd'T'HH:mm:ss]","source":"[Source Name]"},],"blood_pressure_sys":[Repeat with bpSys...],"blood_pressure_dia":[Repeat with bpDia...],"blood_oxygen":[Repeat with spo2...],"weight":[Repeat with weight...],"blood_glucose":[Repeat with glucose...],"steps":[Repeat with steps...],"resting_heart_rate":[Repeat with restingHR...],"sleep":[Repeat with sleep...],"hrv":[Repeat with hrv...],"active_energy":[Repeat with activeEnergy...]}}
```

> **Easier approach:** Instead of manually building JSON, use the **Dictionary** action:
> - Create a Dictionary with keys for each health type
> - For each type, use **Repeat with Each** to build a list of value dictionaries
> - Use **Get Dictionary Value** and convert to JSON text

13. **Save File** action:
    - Save to: **iCloud Drive / Nova / health /**
    - Filename: `health-[Current Date format: yyyy-MM-dd].json`
    - Ask Where to Save: **OFF**
    - Overwrite: **ON**

## Simpler Alternative: Use "Health Auto Export" App

If building the Shortcut is tedious, the free app **Health Auto Export** (by Markus Mayer)
on the App Store can:
- Export all health data types on a daily schedule
- Save as JSON to iCloud Drive
- Configure export path to `Nova/health/`
- Runs in background automatically

This is the path of least resistance.

## Expected JSON Format

Nova expects files in `iCloud Drive/Nova/health/` named `health-YYYY-MM-DD.json`:

```json
{
  "date": "2026-04-12",
  "readings": {
    "heart_rate": [
      {"value": 72, "unit": "bpm", "date": "2026-04-12T08:30:00", "source": "Apple Watch"},
      {"value": 68, "unit": "bpm", "date": "2026-04-12T09:15:00", "source": "Apple Watch"}
    ],
    "blood_pressure_sys": [
      {"value": 122, "unit": "mmHg", "date": "2026-04-12T07:00:00", "source": "Withings"}
    ],
    "blood_pressure_dia": [
      {"value": 78, "unit": "mmHg", "date": "2026-04-12T07:00:00", "source": "Withings"}
    ],
    "blood_oxygen": [
      {"value": 97, "unit": "%", "date": "2026-04-12T06:00:00", "source": "Apple Watch"}
    ],
    "weight": [
      {"value": 185.5, "unit": "lbs", "date": "2026-04-12T06:30:00", "source": "Withings Scale"}
    ],
    "steps": [
      {"value": 8432, "unit": "steps", "date": "2026-04-12T23:00:00", "source": "iPhone"}
    ],
    "sleep": [
      {"stage": "core", "duration_min": 180, "start": "2026-04-11T23:30:00", "end": "2026-04-12T02:30:00", "source": "Apple Watch"},
      {"stage": "deep", "duration_min": 45, "start": "2026-04-12T02:30:00", "end": "2026-04-12T03:15:00", "source": "Apple Watch"},
      {"stage": "rem", "duration_min": 60, "start": "2026-04-12T03:15:00", "end": "2026-04-12T04:15:00", "source": "Apple Watch"}
    ],
    "resting_heart_rate": [
      {"value": 62, "unit": "bpm", "date": "2026-04-12T06:00:00", "source": "Apple Watch"}
    ],
    "hrv": [
      {"value": 42, "unit": "ms", "date": "2026-04-12T06:00:00", "source": "Apple Watch"}
    ],
    "blood_glucose": [
      {"value": 105, "unit": "mg/dL", "date": "2026-04-12T07:30:00", "source": "Dexcom"}
    ],
    "active_energy": [
      {"value": 450, "unit": "kcal", "date": "2026-04-12T23:00:00", "source": "Apple Watch"}
    ]
  }
}
```

## Verification

After the first export, check on the Mac:
```bash
ls ~/Library/Mobile\ Documents/com~apple~CloudDocs/Nova/health/
python3 ~/.openclaw/scripts/nova_health_monitor.py --raw
```

## What Happens Next

Nova's `nova_health_monitor.py` runs every 4 hours via cron:
1. Reads JSON files from iCloud Drive
2. Stores daily summaries in vector memory (source=`apple_health`)
3. Checks readings against alert thresholds
4. Alerts Jordan's DM if anything is concerning (once per day)

Health queries ("what was my blood pressure this week?") route through
the intent router as `health_query` → LOCAL model only → searches
vector memory for `apple_health` entries. Never touches OpenRouter.
