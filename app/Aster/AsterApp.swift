import SwiftUI

@main
struct AsterApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(.dark)
                #if ASTER_SYSTEM_ENGINE
                .task {
                    if CommandLine.arguments.contains("--linux") {
                        SystemEngine.bootLinux()
                    }
                }
                #endif
        }
    }
}
