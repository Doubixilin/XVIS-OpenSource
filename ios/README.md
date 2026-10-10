# Build the iPhone app

Open `XVIS.xcodeproj` in Xcode with an iOS 26 or newer SDK. Deployment target is iOS 17; newer visual APIs have runtime availability checks. The app uses SwiftUI and Apple frameworks without third-party Swift packages.

## Simulator

From the repository root:

```sh
xcodebuild -project ios/XVIS.xcodeproj -scheme XVIS \
  -configuration Debug -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath local_private/ios-build \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=YES build
```

For tests, use an available device ID from `xcrun simctl list devices available`:

```sh
xcodebuild -project ios/XVIS.xcodeproj -scheme XVIS \
  -configuration Debug -destination 'platform=iOS Simulator,id=SIMULATOR_ID' \
  -derivedDataPath local_private/ios-tests \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=YES test
```

Keep ad hoc signing enabled when running the simulator; an unsigned app may fail Keychain access. An unsigned compile alone is not runtime validation.

## Your iPhone

Choose your own development team and a unique bundle identifier for the app and test target. The checked-in identifier `org.example.xvis` is a placeholder. Credentials use a Keychain service derived from the bundle identifier. No development team, certificate or provisioning profile is included.

The project declares the push-notification entitlement. Provision that capability with your Apple account if using APNs. Debug uses `development` / `sandbox`; Release uses `production`. A successful build does not prove real APNs delivery. The Hub certificate, topic and environment must match the signed app.

On first launch, open Settings and enter **your own HTTPS Hub address** (for example `https://example.com/xvis/`) and its App credential. There is no default live server. System certificate verification remains enabled; self-signed TLS requires an explicitly trusted CA on the simulator or device.

Use the registered `p-xvis-test` project first. If it is unavailable, the app does not switch to a different real project automatically. Existing records remain tied to their original device and workspace.
