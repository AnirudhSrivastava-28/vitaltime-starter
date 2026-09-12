import SwiftUI

struct DecisionRevealView: View {
    @ObservedObject var appState: AppState

    var body: some View {
        // Wrapped in NavigationStack because ContentView presents this
        // screen standalone (via the `Group { switch ... }` in body),
        // not pushed onto an existing nav stack — without one the
        // `.navigationTitle` below would silently do nothing.
        NavigationStack {
            VStack(spacing: 20) {
                if let decision = appState.latestDecision {
                    Text("Routing result")
                        .font(.title2.bold())

                    RoundedRectangle(cornerRadius: 16)
                        .fill(Color.tierColor(for: decision.tier))
                        .frame(height: 140)
                        .overlay {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Tier \(decision.tier)")
                                    .font(.headline)
                                Text("Assigned staff: \(decision.assigned_staff_id ?? "None")")
                                Text("ETA: \(decision.eta.map { String(format: "%.1f", $0) } ?? "n/a") min")
                                Text("Fatigue at assignment: \(decision.fatigue_score_at_assignment.map { String(format: "%.1f", $0) } ?? "n/a")")
                                Text("Status: \(decision.status)")
                            }
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding()
                        }

                    Button("Open facility view") {
                        appState.continueToMainApp()
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    Text("No decision available")
                }

                Spacer()
            }
            .padding()
            .navigationTitle("Decision")
        }
    }
}
