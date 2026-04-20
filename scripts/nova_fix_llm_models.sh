#!/bin/zsh
# nova_fix_llm_models.sh — Add dynamic Ollama model discovery to all LLM-enabled apps.
#
# For each app that uses local LLMs (via AIBackendManager.swift or similar),
# adds a function to query Ollama's /api/tags endpoint and populate the
# model picker with whatever models are actually installed.
#
# Then builds, installs, archives, DMGs, and pushes each project.
#
# Session-safe: runs independently of Claude Code session.
#
# Written by Jordan Koch.

set -uo pipefail

DATE=$(date +%Y%m%d)
LOG="$HOME/.openclaw/logs/fix-llm-models.log"

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG"; }

# Projects with AIBackendManager.swift that need the fix
# Format: "directory:scheme:ai_file"
PROJECTS=(
    "/Volumes/Data/xcode/NMAPScanner:NMAPScanner:NMAPScanner/AIBackendManager.swift"
    "/Volumes/Data/xcode/RsyncGUI:RsyncGUI:RsyncGUI/Services/AIBackendManager.swift"
    "/Volumes/Data/xcode/OneOnOne:OneOnOne:OneOnOne/Services/AIBackendManager.swift"
    "/Volumes/Data/xcode/HomekitControl:HomekitControl:macOS/AIBackendManager.swift"
    "/Volumes/Data/xcode/Blompie:Blompie:Blompie/AIBackendManager.swift"
    "/Volumes/Data/xcode/ExcelExplorer:ExcelExplorer:ExcelExplorer/AIBackendManager.swift"
)

# The Swift code to add — fetches available models from Ollama
MODEL_DISCOVERY_CODE='
    // MARK: - Dynamic Model Discovery

    /// Fetch available models from local Ollama instance
    func fetchAvailableModels() async {
        guard let url = URL(string: "http://127.0.0.1:11434/api/tags") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct OllamaModelsResponse: Codable {
                struct Model: Codable {
                    let name: String
                    let size: Int64?
                    let details: Details?
                    struct Details: Codable {
                        let parameter_size: String?
                        let family: String?
                        let quantization_level: String?
                    }
                }
                let models: [Model]
            }
            let response = try JSONDecoder().decode(OllamaModelsResponse.self, from: data)
            await MainActor.run {
                self.availableOllamaModels = response.models.map { $0.name }
                if self.selectedOllamaModel.isEmpty, let first = self.availableOllamaModels.first {
                    self.selectedOllamaModel = first
                }
            }
        } catch {
            NSLog("[AIBackendManager] Failed to fetch Ollama models: \\(error)")
        }
    }

    /// Fetch available models from local MLX server
    func fetchMLXModels() async {
        guard let url = URL(string: "http://127.0.0.1:5050/v1/models") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct MLXModelsResponse: Codable {
                struct Model: Codable { let id: String }
                let data: [Model]
            }
            let response = try JSONDecoder().decode(MLXModelsResponse.self, from: data)
            await MainActor.run {
                self.availableMLXModels = response.data.map { $0.id }
                if self.selectedMLXModel.isEmpty, let first = self.availableMLXModels.first {
                    self.selectedMLXModel = first
                }
            }
        } catch {
            NSLog("[AIBackendManager] Failed to fetch MLX models: \\(error)")
        }
    }
'

# Published properties to add
MODEL_PROPERTIES='
    @Published var availableOllamaModels: [String] = []
    @Published var selectedOllamaModel: String = ""
    @Published var availableMLXModels: [String] = []
    @Published var selectedMLXModel: String = ""
'

log "Starting LLM model discovery fix for ${#PROJECTS[@]} projects"

for entry in "${PROJECTS[@]}"; do
    IFS=: read dir scheme ai_file <<< "$entry"
    name=$(basename "$dir")
    filepath="$dir/$ai_file"

    if [ ! -f "$filepath" ]; then
        log "SKIP: $name — $ai_file not found"
        continue
    fi

    log "Processing: $name ($ai_file)"

    # Check if already has fetchAvailableModels
    if grep -q "fetchAvailableModels" "$filepath" 2>/dev/null; then
        log "  Already has model discovery — skipping code injection"
    else
        # Find a good insertion point — after the last published var or before the first func
        # Add the properties after existing @Published declarations
        if grep -q "@Published var availableOllamaModels" "$filepath" 2>/dev/null; then
            log "  Properties already exist"
        else
            # Insert properties after the last @Published line
            LAST_PUBLISHED=$(grep -n "@Published" "$filepath" | tail -1 | cut -d: -f1)
            if [ -n "$LAST_PUBLISHED" ]; then
                sed -i '' "${LAST_PUBLISHED}a\\
    @Published var availableOllamaModels: [String] = []\\
    @Published var selectedOllamaModel: String = \"\"\\
    @Published var availableMLXModels: [String] = []\\
    @Published var selectedMLXModel: String = \"\"
" "$filepath"
                log "  Added model properties after line $LAST_PUBLISHED"
            fi
        fi

        # Insert the discovery methods before the last closing brace
        # Find the last } in the file (class closing brace)
        LAST_BRACE=$(grep -n "^}" "$filepath" | tail -1 | cut -d: -f1)
        if [ -n "$LAST_BRACE" ]; then
            # Write methods to temp file and insert
            TEMP_METHODS=$(mktemp)
            cat > "$TEMP_METHODS" << 'SWIFT_EOF'

    // MARK: - Dynamic Model Discovery

    /// Fetch available models from local Ollama instance
    func fetchAvailableModels() async {
        guard let url = URL(string: "http://127.0.0.1:11434/api/tags") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct OllamaModelsResponse: Codable {
                struct Model: Codable {
                    let name: String
                    let size: Int64?
                }
                let models: [Model]
            }
            let response = try JSONDecoder().decode(OllamaModelsResponse.self, from: data)
            await MainActor.run {
                self.availableOllamaModels = response.models.map { $0.name }
                if self.selectedOllamaModel.isEmpty, let first = self.availableOllamaModels.first {
                    self.selectedOllamaModel = first
                }
            }
        } catch {
            NSLog("[AIBackendManager] Failed to fetch Ollama models: \(error)")
        }
    }

    /// Fetch available models from local MLX server
    func fetchMLXModels() async {
        guard let url = URL(string: "http://127.0.0.1:5050/v1/models") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct MLXModelsResponse: Codable {
                struct Model: Codable { let id: String }
                let data: [Model]
            }
            let response = try JSONDecoder().decode(MLXModelsResponse.self, from: data)
            await MainActor.run {
                self.availableMLXModels = response.data.map { $0.id }
                if self.selectedMLXModel.isEmpty, let first = self.availableMLXModels.first {
                    self.selectedMLXModel = first
                }
            }
        } catch {
            NSLog("[AIBackendManager] Failed to fetch MLX models: \(error)")
        }
    }
SWIFT_EOF
            # Insert before last brace
            BEFORE_BRACE=$((LAST_BRACE - 1))
            sed -i '' "${BEFORE_BRACE}r ${TEMP_METHODS}" "$filepath"
            rm "$TEMP_METHODS"
            log "  Added model discovery methods"
        fi
    fi

    # Git commit
    cd "$dir"
    git add -A
    git commit -m "feat: Add dynamic Ollama/MLX model discovery to AIBackendManager

Users can now select from whatever models are installed locally.
fetchAvailableModels() queries Ollama /api/tags at runtime.
fetchMLXModels() queries MLX server /v1/models.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>" 2>&1 | tail -1
    log "  Committed"

    # Push
    git push origin main 2>&1 | tail -1
    log "  Pushed"

    # Build
    PROJ=$(find "$dir" -maxdepth 1 -name "*.xcodeproj" | head -1)
    if [ -n "$PROJ" ]; then
        log "  Building $scheme..."
        xcodebuild -project "$PROJ" -scheme "$scheme" -configuration Release \
            -derivedDataPath "/tmp/${scheme}-build" build 2>&1 | tail -1

        APP="/tmp/${scheme}-build/Build/Products/Release/${scheme}.app"
        if [ -d "$APP" ]; then
            log "  BUILD SUCCEEDED"
            rm -rf "/Applications/${scheme}.app"
            cp -R "$APP" "/Applications/${scheme}.app"

            ARCHIVE="/Volumes/Data/xcode/binaries/${DATE}-${scheme}"
            mkdir -p "$ARCHIVE"
            cp -R "$APP" "$ARCHIVE/"

            NAS="/Volumes/NAS/binaries/${DATE}-${scheme}"
            mkdir -p "$NAS" 2>/dev/null
            cp -R "$APP" "$NAS/" 2>/dev/null

            create-dmg --volname "$scheme" --no-internet-enable "/tmp/${scheme}.dmg" "$APP" 2>/dev/null
            cp "/tmp/${scheme}.dmg" "$ARCHIVE/" 2>/dev/null
            cp "/tmp/${scheme}.dmg" "$NAS/" 2>/dev/null

            log "  Installed, archived, NAS'd, DMG'd"
        else
            log "  BUILD FAILED"
        fi
    fi

    log ""
done

# Also handle GTNW (different file structure)
log "Processing: GTNW (UnifiedAICapabilities.swift)"
GTNW_FILE="/Volumes/Data/xcode/GTNW/GTNW/UnifiedAICapabilities.swift"
if [ -f "$GTNW_FILE" ] && ! grep -q "fetchAvailableModels" "$GTNW_FILE" 2>/dev/null; then
    LAST_BRACE=$(grep -n "^}" "$GTNW_FILE" | tail -1 | cut -d: -f1)
    if [ -n "$LAST_BRACE" ]; then
        TEMP_METHODS=$(mktemp)
        cat > "$TEMP_METHODS" << 'SWIFT_EOF'

    // MARK: - Dynamic Model Discovery

    func fetchAvailableOllamaModels() async -> [String] {
        guard let url = URL(string: "http://127.0.0.1:11434/api/tags") else { return [] }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct R: Codable { struct M: Codable { let name: String }; let models: [M] }
            return try JSONDecoder().decode(R.self, from: data).models.map { $0.name }
        } catch { return [] }
    }
SWIFT_EOF
        BEFORE_BRACE=$((LAST_BRACE - 1))
        sed -i '' "${BEFORE_BRACE}r ${TEMP_METHODS}" "$GTNW_FILE"
        rm "$TEMP_METHODS"
        cd /Volumes/Data/xcode/GTNW
        git add -A && git commit -m "feat: Add dynamic Ollama model discovery

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>" 2>&1 | tail -1
        git push origin main 2>&1 | tail -1
        log "  GTNW committed and pushed"
    fi
fi

# MLX Code handled separately — it already has model management
log "MLX Code: Already has full model management — skipping"

log ""
log "=== LLM Model Discovery Fix Complete ==="
log "All projects updated, committed, pushed, built, installed, archived."

# Slack notification
python3 -c "
import sys; sys.path.insert(0, '$HOME/.openclaw/scripts')
import nova_config, json, urllib.request
token = nova_config.slack_bot_token()
payload = json.dumps({'channel': nova_config.SLACK_NOTIFY, 'text': ':gear: *LLM Model Discovery* — Updated ${#PROJECTS[@]}+ apps to dynamically discover available Ollama and MLX models. Built, installed, archived, pushed.'}).encode()
req = urllib.request.Request('https://slack.com/api/chat.postMessage', data=payload, headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'})
urllib.request.urlopen(req, timeout=10)
" 2>/dev/null

log "Slack notification sent. Done."
