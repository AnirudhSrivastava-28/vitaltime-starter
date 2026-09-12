import Foundation
import SwiftUI

struct RouteEventResponse: Codable, Equatable {
    let event_id: String
    let room: String
    let tier: Int
    let assigned_staff_id: String?
    let eta: Double?
    let fatigue_score_at_assignment: Double?
    let status: String
    // Only ever non-nil while status == "pending" AND every qualified
    // candidate is currently uninterruptible-busy. Not a reservation —
    // see backend engine.py's _predict_next_available_staff docstring.
    // Optional with a default so decoding an older backend response
    // (before this field existed) still succeeds.
    let predicted_next_staff_id: String?
}

struct StaffPositionResponse: Codable, Equatable {
    let room: String
}

/// Mirrors backend/app/routers/routing.py's TransitPlanResponse.
///
/// Currently decoded but not rendered — the iOS app is a "smart remote,"
/// so it just needs to *have* the transit truth from the backend, not
/// visualize it (the dashboard already does that). Storing it here means
/// future in-app views (e.g. "en route to Room X, ETA 45s") can read it
/// without another round of model changes.
///
/// `hop_durations_seconds` are already scaled to real wall-clock seconds
/// by the backend (`DEMO_TIME_SCALE`). Any client-side timing math should
/// use these, never `hop_times` (which are unscaled sim-minutes, kept
/// only for backwards compatibility with older callers).
struct TransitPlanResponse: Codable, Equatable {
    let path: [String]
    let hop_times: [Double]
    let hop_durations_seconds: [Double]
    let departure_time: String
    let arrival_time: String
    let mode: String
}

/// Mirrors backend `StaffPositionItem` exactly. `home_room` and `transit`
/// were added on the backend to support the dashboard's dead-reckoning
/// animation; on iOS they're decoded so the model stays a faithful
/// snapshot of backend truth, even though no view renders them yet.
struct StaffPosition: Codable, Identifiable, Equatable {
    let staff_id: String
    let role: String
    let qualification_level: String
    let shift_start: String
    let current_position: StaffPositionResponse
    let home_room: String
    let status: String
    let current_event_id: String?
    let hours_in_shift: Double
    let fatigue_score: Double
    let transit: TransitPlanResponse?

    var id: String { staff_id }
}

struct ClearAssignmentResponse: Codable, Equatable {
    let event_id: String
    let status: String
    let reassigned_pending: RouteEventResponse?
}

struct ResetSimulationResponse: Codable, Equatable {
    let status: String
}

/// Decodes GET /events and the `events` array inside GET /dashboard-state
/// (and the same payload pushed over /dashboard-stream). Mirrors backend
/// EventItem exactly — including eta and fatigue_score_at_assignment,
/// which are now persisted on the Event itself server-side (not just
/// returned once by /route-event), so a fresh poll or SSE frame always
/// has them, not only whatever the app cached at submission time.
struct EventItem: Codable, Identifiable, Equatable {
    let event_id: String
    let room: String
    let tier: Int
    let symptom_tags: [String]
    let status: String
    let assigned_staff_id: String?
    let submitted_at: String
    let tending_until: String?
    let eta: Double?
    let fatigue_score_at_assignment: Double?
    let predicted_next_staff_id: String?

    var id: String { event_id }
}

/// GET /dashboard-state — one atomic snapshot of staff + events from a
/// single backend read. The SSE stream (/dashboard-stream) pushes this
/// same shape. The app never maintains its own copy of simulation state:
/// the backend is the sole source of truth, the app just reflects it.
struct DashboardStateResponse: Codable, Equatable {
    let server_time: String
    let time_scale: Double
    let staff: [StaffPosition]
    let events: [EventItem]
}

struct ActiveEvent: Identifiable, Equatable {
    let id: String
    let room: String
    let symptomTags: [String]
    let submittedAt: String
    let tier: Int
    let assignedStaffId: String?
    let eta: Double?
    let fatigueScoreAtAssignment: Double?
    var status: String
    let predictedNextStaffId: String?

    init(id: String, room: String, symptomTags: [String], submittedAt: String, tier: Int, assignedStaffId: String?, eta: Double?, fatigueScoreAtAssignment: Double?, status: String, predictedNextStaffId: String? = nil) {
        self.id = id
        self.room = room
        self.symptomTags = symptomTags
        self.submittedAt = submittedAt
        self.tier = tier
        self.assignedStaffId = assignedStaffId
        self.eta = eta
        self.fatigueScoreAtAssignment = fatigueScoreAtAssignment
        self.status = status
        self.predictedNextStaffId = predictedNextStaffId
    }

    init(from response: RouteEventResponse, room: String, symptomTags: [String], submittedAt: String) {
        self.init(
            id: response.event_id,
            room: room,
            symptomTags: symptomTags,
            submittedAt: submittedAt,
            tier: response.tier,
            assignedStaffId: response.assigned_staff_id,
            eta: response.eta,
            fatigueScoreAtAssignment: response.fatigue_score_at_assignment,
            status: response.status,
            predictedNextStaffId: response.predicted_next_staff_id
        )
    }

    /// The path that matters going forward: every ActiveEvent the app
    /// displays after the initial submission is built from a polled
    /// EventItem — never hand-patched from a cached response.
    init(from item: EventItem) {
        self.init(
            id: item.event_id,
            room: item.room,
            symptomTags: item.symptom_tags,
            submittedAt: item.submitted_at,
            tier: item.tier,
            assignedStaffId: item.assigned_staff_id,
            eta: item.eta,
            fatigueScoreAtAssignment: item.fatigue_score_at_assignment,
            status: item.status,
            predictedNextStaffId: item.predicted_next_staff_id
        )
    }
}

enum AppScreen: Equatable {
    case eventForm
    case decisionReveal
    case mainApp
}

extension Date {
    var iso8601String: String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: self)
    }
}

extension Color {
    static func tierColor(for tier: Int) -> Color {
        switch tier {
        case 1:
            return .red
        case 2:
            return .orange
        case 3:
            return .green
        default:
            return .gray
        }
    }
}
