import SwiftUI

struct StaffPhoneView: View {
    let staff: StaffPosition
    let activeEvents: [ActiveEvent]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle()
                    .fill(staff.status == "busy" ? Color.red.opacity(0.8) : Color.green.opacity(0.8))
                    .frame(width: 14, height: 14)
                Text(staff.staff_id)
                    .font(.title3.bold())
                Spacer()
                Text(staff.role)
                    .font(.caption)
                    .padding(6)
                    .background(Color.gray.opacity(0.15))
                    .clipShape(Capsule())
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Location: \(staff.current_position.room)")
                Text("Qualification: \(staff.qualification_level)")
                Text("Status: \(staff.status)")
                Text("Fatigue: \(String(format: "%.1f", staff.fatigue_score))")
                Text("Current event: \(staff.current_event_id ?? "none")")
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)

            if let event = activeEvents.first(where: { $0.assignedStaffId == staff.staff_id }) {
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color.tierColor(for: event.tier))
                    .frame(height: 70)
                    .overlay {
                        VStack(alignment: .leading) {
                            Text("Active assignment")
                                .font(.subheadline.bold())
                            Text("Room \(event.room) • Tier \(event.tier)")
                                .font(.caption)
                        }
                        .foregroundStyle(.white)
                        .padding()
                    }
            }
        }
        .padding()
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .shadow(radius: 2)
    }
}
