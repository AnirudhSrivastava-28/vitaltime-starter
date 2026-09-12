import SwiftUI

struct MainAppView: View {
    @ObservedObject var appState: AppState

    var body: some View {
        // ZStack instead of a bare TabView so the "report another event"
        // control is a persistent overlay, not something buried inside one
        // tab's toolbar — it needs to be reachable no matter which tab
        // (Map or Staff) the user is currently on.
        ZStack(alignment: .bottomTrailing) {
            TabView(selection: $appState.selectedMainTab) {
                FacilityMapView(livePositionsModel: appState.livePositionsModel, appState: appState)
                    .tabItem {
                        Label("Map", systemImage: "map")
                    }
                    .tag(0)

                StaffRosterView(appState: appState)
                    .tabItem {
                        Label("Staff", systemImage: "person.2")
                    }
                    .tag(1)
            }

            ReportEmergencyButton {
                appState.reportAnotherEvent()
            }
            .padding(.trailing, 20)
            .padding(.bottom, 78) // clears the tab bar
        }
    }
}

private struct ReportEmergencyButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title2.bold())
                .foregroundStyle(.white)
                .frame(width: 60, height: 60)
                .background(Color.red)
                .clipShape(Circle())
                .shadow(color: .black.opacity(0.25), radius: 6, y: 3)
        }
        .accessibilityLabel("Report another event")
    }
}

struct StaffRosterView: View {
    @ObservedObject var appState: AppState

    var body: some View {
        NavigationStack {
            List(appState.livePositionsModel.staffPositions) { staff in
                NavigationLink(destination: StaffPhoneView(staff: staff, activeEvents: appState.activeEvents)) {
                    HStack {
                        Circle()
                            .fill(staff.status == "busy" ? Color.red.opacity(0.8) : Color.green.opacity(0.8))
                            .frame(width: 12, height: 12)
                        VStack(alignment: .leading) {
                            Text(staff.staff_id)
                                .font(.subheadline.bold())
                            Text(staff.role)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("Staff roster")
        }
    }
}
