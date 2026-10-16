#!/bin/zsh
# Build, ad-hoc sign, install, and export the Nova Minion screensaver.
#   tools/build.sh                 # build + sign + install to ~/Library/Screen Savers + export binaries
#   NO_INSTALL=1 tools/build.sh    # skip the install step
set -euo pipefail
cd "${0:A:h}/.."
DERIVED=/Volumes/Data/xcode/DerivedData/NovaMinionSaver
VERSION=$(awk -F'"' '/MARKETING_VERSION/{print $2; exit}' project.yml)

xcodegen generate --quiet
xcodebuild -project NovaMinionSaver.xcodeproj -scheme NovaMinion -configuration Release \
  -destination 'generic/platform=macOS' -derivedDataPath "$DERIVED" build -quiet 2>&1 | grep -v -e CoreSimulator -e appintentsmetadata || true
PRODUCT="$DERIVED/Build/Products/Release/NovaMinion.saver"
[[ -d "$PRODUCT" ]] || { echo "build failed"; exit 1; }

codesign --force --deep -s - "$PRODUCT"
codesign --verify --deep --strict "$PRODUCT"
echo "principal class: $(/usr/libexec/PlistBuddy -c 'Print :NSPrincipalClass' "$PRODUCT/Contents/Info.plist")"

# ditto merges into an existing bundle; the bundle has a fixed file set so that is a clean replace.
if [[ -z "${NO_INSTALL:-}" ]]; then
  mkdir -p "$HOME/Library/Screen Savers"
  ditto "$PRODUCT" "$HOME/Library/Screen Savers/NovaMinion.saver"
  echo "installed to ~/Library/Screen Savers/NovaMinion.saver"
fi

ZIP="NovaMinion-saver-$VERSION.zip"
for dest in /Volumes/Data/xcode/binaries /Volumes/NAS/binaries; do
  if [[ -d "$dest" ]]; then
    ditto "$PRODUCT" "$dest/NovaMinion.saver"
    ditto -c -k --keepParent "$PRODUCT" "$dest/$ZIP"
    echo "exported to $dest ($ZIP)"
  else
    echo "skip export: $dest not mounted"
  fi
done
