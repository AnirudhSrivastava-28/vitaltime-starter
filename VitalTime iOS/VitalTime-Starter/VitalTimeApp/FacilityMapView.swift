import SwiftUI

struct FacilityMapView: View {
    @ObservedObject var livePositionsModel: LivePositionsModel
    @ObservedObject var appState: AppState

    var body: some View {
        NavigationStack {
            List {
                Section("Active events") {
                    let activeEvents = appState.activeEvents.filter { $0.status != "resolved" }
                    if activeEvents.isEmpty {
                        Text("No active events yet.")
                            .foregroundStyle(.secondary)
                            .font(.subheadline)
                    } else {
                        ForEach(activeEvents) { event in
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Text("T\(event.tier)")
                                        .foregroundStyle(Color.tierColor(for: event.tier))
                                        .font(.caption.bold())
                                    Text("Room \(event.room)")
                                        .font(.headline)
                                    Spacer()
                                    Text(event.status)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Text("Assigned: \(event.assignedStaffId ?? "pending")")
                                    .font(.subheadline)
                                // Only ever shown when status == "pending"
                                // AND the backend found every qualified
                                // candidate uninterruptible-busy — see
                                // ActiveEvent.predictedNextStaffId. Styled
                                // as a forecast (italic, secondary), not a
                                // confirmed assignment.
                                if event.assignedStaffId == nil, let predicted = event.predictedNextStaffId {
                                    Text("Next up: \(predicted)")
                                        .font(.caption)
                                        .italic()
                                        .foregroundStyle(.secondary)
                                }
                                Text("Tier \(event.tier) • \(event.symptomTags.joined(separator: ", "))")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                Section("Staff positions") {
                    if livePositionsModel.isLoading && livePositionsModel.staffPositions.isEmpty {
                        HStack {
                            ProgressView()
                            Text("Loading staff…")
                                .foregroundStyle(.secondary)
                        }
                    } else if livePositionsModel.staffPositions.isEmpty {
                        Text("No staff data available.")
                            .foregroundStyle(.secondary)
                            .font(.subheadline)
                    } else {
                        ForEach(livePositionsModel.staffPositions) { staff in
                            HStack {
                                Text(staff.status == "busy" ? "BUSY" : "READY")
                                    .foregroundStyle(staff.status == "busy" ? Color.red : Color.green)
                                    .font(.caption.bold())
                                VStack(alignment: .leading) {
                                    Text(staff.staff_id)
                                        .font(.subheadline.bold())
                                    Text("\(staff.role) • \(staff.current_position.room)")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Clear") {
                                    appState.clearAssignment(for: staff)
                                }
                                .buttonStyle(.bordered)
                                .disabled(staff.status != "busy")
                            }
                        }
                    }
                }
            }
            .navigationTitle("Facility map")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Label(
                        livePositionsModel.isStreamConnected ? "Live" : "Reconnecting",
                        systemImage: livePositionsModel.isStreamConnected ? "circle.fill" : "arrow.clockwise"
                    )
                    .foregroundStyle(livePositionsModel.isStreamConnected ? .green : .orange)
                    .font(.caption)
                }
            }
        }
    }
}
