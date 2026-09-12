import SwiftUI

struct EventFormView: View {
    @ObservedObject var appState: AppState
    @State private var selectedRoom = EventFormView.rooms.first!
    @State private var selectedEventIndex = 0

    // Matches app/routing/tier.py TIER_KEYWORDS exactly, so every option here
    // is guaranteed to classify the way its label says it will.
    static let eventOptions: [(label: String, tier: Int, tags: [String])] = [
        ("Not breathing", 1, ["not breathing"]),
        ("Unresponsive", 1, ["unresponsive"]),
        ("Cardiac arrest", 1, ["cardiac arrest"]),
        ("Choking", 1, ["choking"]),
        ("Seizure", 1, ["seizure"]),
        ("Possible fall", 2, ["possible fall"]),
        ("Confused / disoriented", 2, ["confused"]),
        ("Chest pain", 2, ["chest pain"]),
        ("Shortness of breath", 2, ["shortness of breath"]),
        ("High fever", 2, ["high fever"]),
        ("Needs bandage / minor bleeding", 3, ["needs bandage"]),
        ("Escort request", 3, ["escort"]),
        ("Medication refill", 3, ["medication refill"]),
        ("Bathroom assist", 3, ["bathroom assist"]),
        ("Comfort check", 3, ["comfort check"]),
    ]

    // Must match app/routing/eta.py's _FACILITY_EDGES exactly (NS + 101-112).
    // Rooms 111-112 were added to the backend graph (and the dashboard's
    // ROOM_COORDS) to close a client/server range mismatch; the picker
    // needs to mirror that or the UI silently truncates the facility.
    // Anything outside the backend graph falls back to a flat 5-minute ETA
    // server-side, which fails Tier 1's 2-minute window outright — the
    // "smart remote" should never be able to send a room the backend
    // doesn't actually know about.
    static let rooms: [String] = (101...112).map(String.init)

    private var selectedEvent: (label: String, tier: Int, tags: [String]) {
        Self.eventOptions[selectedEventIndex]
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Resident event") {
                    Picker("Room", selection: $selectedRoom) {
                        ForEach(Self.rooms, id: \.self) { room in
                            Text(room).tag(room)
                        }
                    }

                    Picker("Event", selection: $selectedEventIndex) {
                        ForEach(Self.eventOptions.indices, id: \.self) { index in
                            Label {
                                Text(Self.eventOptions[index].label)
                            } icon: {
                                Circle()
                                    .fill(Color.tierColor(for: Self.eventOptions[index].tier))
                                    .frame(width: 10, height: 10)
                            }
                            .tag(index)
                        }
                    }
                }

                Section {
                    HStack {
                        Circle()
                            .fill(Color.tierColor(for: selectedEvent.tier))
                            .frame(width: 10, height: 10)
                        Text("Tier \(selectedEvent.tier) event")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }

                Section {
                    Button(action: submit) {
                        if appState.isSubmitting {
                            HStack {
                                ProgressView()
                                Text("Submitting…")
                            }
                        } else {
                            Text("Submit event")
                                .frame(maxWidth: .infinity)
                        }
                    }
                    .disabled(appState.isSubmitting)
                }
            }
            .navigationTitle("VitalTime")
            .toolbar {
                // Only offered once there's somewhere to cancel back to —
                // on the very first launch (no events yet) there's no
                // mainApp state worth returning to without submitting.
                if !appState.activeEvents.isEmpty {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") {
                            appState.currentScreen = .mainApp
                        }
                        .disabled(appState.isSubmitting)
                    }
                }
            }
        }
    }

    private func submit() {
        let submittedAt = Date().iso8601String
        appState.submitEvent(room: selectedRoom, symptomTags: selectedEvent.tags, submittedAt: submittedAt)
    }
}
