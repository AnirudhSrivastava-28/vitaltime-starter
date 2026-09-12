import SwiftUI
import Combine
import UIKit

struct ContentView: View {
    @StateObject private var appState = AppState()

    var body: some View {
        Group {
            switch appState.currentScreen {
            case .eventForm:
                EventFormView(appState: appState)
            case .decisionReveal:
                DecisionRevealView(appState: appState)
            case .mainApp:
                MainAppView(appState: appState)
            }
        }
        .alert("Notice", isPresented: Binding(
            get: { appState.errorMessage != nil },
            set: { _ in appState.errorMessage = nil }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(appState.errorMessage ?? "Unknown error")
        }
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var currentScreen: AppScreen = .eventForm
    // The backend is the sole source of truth for what events exist —
    // populated exclusively by LivePositionsModel (SSE stream + bootstrap
    // fetch). Nothing in this class appends, mutates, or removes entries
    // directly.
    @Published var activeEvents: [ActiveEvent] = []
    @Published var latestDecision: RouteEventResponse?
    @Published var isSubmitting = false
    @Published var errorMessage: String?
    // Hoisted out of MainAppView so re-entering .mainApp doesn't reset
    // the user back to the first tab — MainAppView gets torn down and
    // rebuilt every time currentScreen swaps away and back, so any
    // @State living inside it wouldn't survive that round trip. This
    // does, because it lives here.
    @Published var selectedMainTab = 0

    private let apiService: APIService
    let livePositionsModel: LivePositionsModel

    init(apiService: APIService = APIService()) {
        self.apiService = apiService
        self.livePositionsModel = LivePositionsModel(apiService: apiService)

        // Wired as a callback (rather than AppState reaching into
        // livePositionsModel.activeEvents itself) so activeEvents stays a
        // real @Published property ON AppState. A computed property that
        // merely forwarded to livePositionsModel.activeEvents would NOT
        // trigger AppState's own objectWillChange when the stream
        // updates it — views observing `@ObservedObject var appState`
        // (most of them) simply wouldn't refresh. This way every push
        // republishes through AppState itself, so every existing call
        // site (appState.activeEvents) stays live-updated.
        self.livePositionsModel.onEventsReceived = { [weak self] events in
            self?.activeEvents = events
        }
    }

    func submitEvent(room: String, symptomTags: [String], submittedAt: String) {
        isSubmitting = true
        errorMessage = nil

        Task {
            do {
                let response = try await apiService.routeEvent(room: room, symptomTags: symptomTags, submittedAt: submittedAt)
                // latestDecision comes straight from this response — fine
                // and correct, it's immediate feedback for the screen the
                // user is looking at right now. activeEvents is NOT
                // hand-appended from it: the backend signals its own
                // change the moment this write lands (see store.py's
                // _signal_change), and the already-open SSE stream pushes
                // the update on its own.
                latestDecision = response
                currentScreen = .decisionReveal

                // Defensive hedge: kick off an immediate /dashboard-state
                // fetch. On a healthy stream this is redundant (the SSE
                // frame lands first), but if the stream is mid-reconnect
                // or the initial signal hasn't propagated yet, this makes
                // sure MainAppView doesn't render with empty staff/events
                // the moment the user taps through the decision reveal.
                await livePositionsModel.refreshNow()
            } catch {
                errorMessage = error.localizedDescription
            }
            isSubmitting = false
        }
    }

    func continueToMainApp() {
        currentScreen = .mainApp
    }

    /// The "go back to submit" entry point, called from a persistent
    /// button in MainAppView. Purely a navigation action — activeEvents,
    /// latestDecision, and the live stream all keep running via appState
    /// regardless of which screen is showing.
    func reportAnotherEvent() {
        errorMessage = nil
        currentScreen = .eventForm
    }

    func clearAssignment(for staff: StaffPosition) {
        guard let event = activeEvents.first(where: { $0.assignedStaffId == staff.staff_id && $0.status != "resolved" }) else {
            return
        }

        Task {
            do {
                _ = try await apiService.clearAssignment(eventId: event.id)
                // Same principle: don't hand-patch activeEvents from this
                // response. The write's own _signal_change() on the
                // backend means the stream pushes the true resulting
                // state (including any reassigned_pending event) on its
                // own, typically before this call even returns.
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}

/// Owns the live connection to the backend's simulation state — staff
/// positions AND events both, from one SSE stream (/dashboard-stream),
/// rather than a Timer polling for either. The backend pushes a full
/// snapshot the instant anything changes (an explicit write, or a
/// time-based transition it discovers on its own ~1s tick); this class
/// just applies whatever it's sent. No local mutation, no bookkeeping —
/// see AppState.onEventsReceived wiring above for why that matters.
///
/// Bootstrap policy: every fresh SSE connect starts with an immediate
/// /dashboard-state fetch, so the UI is never blank waiting on the
/// first SSE frame. Then the stream takes over for updates.
@MainActor
final class LivePositionsModel: ObservableObject {
    @Published var staffPositions: [StaffPosition] = []
    // True whenever there's no data at all yet AND the stream isn't
    // currently connected. Once we've applied at least one snapshot
    // (from bootstrap fetch OR from SSE), this stays false even during
    // subsequent reconnects — the stale data is more useful than a
    // spinner while the stream is restoring.
    @Published var isLoading = true
    @Published var errorMessage: String?

    /// True while an SSE connection is up and receiving. Kept as its own
    /// signal (separate from isLoading) so views can show a subtle
    /// "reconnecting…" indicator without hiding the last-known state.
    @Published var isStreamConnected = false

    var onEventsReceived: (([ActiveEvent]) -> Void)?

    private let apiService: APIService
    private var streamTask: Task<Void, Never>?
    private var lifecycleObservers: [NSObjectProtocol] = []
    private let reconnectDelayNanoseconds: UInt64 = 2_000_000_000
    private let maximumReconnectDelayNanoseconds: UInt64 = 60_000_000_000

    /// Guards against three independent, unsynchronized network paths —
    /// the persistent SSE stream, the bootstrap refreshNow() at the top
    /// of every reconnect, and the defensive post-submit refreshNow() —
    /// all calling apply(_:) with no ordering guarantee relative to each
    /// other. Without this, whichever response lands LAST wins, even if
    /// it's carrying an older snapshot than one already applied a moment
    /// earlier (e.g. a slow bootstrap GET that was in flight before a
    /// submit, resolving after the SSE stream already pushed the
    /// post-submit truth, silently reverting activeEvents back to a
    /// stale state). Backend `server_time` values are naive-UTC
    /// isoformat() strings — Python's isoformat() never trims fractional
    /// digits to variable width (always exactly 6 digits when non-zero,
    /// omitted entirely only when microsecond == 0, which still sorts
    /// correctly as "earlier" against any string with a fraction) — so
    /// plain lexicographic string comparison is a safe, parse-free stand-in
    /// for chronological comparison here, mirroring the same guard
    /// dashboard.html already applies via parsed millisecond timestamps.
    private var lastServerTime: String = ""

    init(apiService: APIService) {
        self.apiService = apiService
        startStreaming()
        observeAppLifecycle()
    }

    /// Start (or restart) the SSE stream. Cancels any existing task
    /// first to guarantee only one is ever running — otherwise the
    /// lifecycle observer firing concurrently with init could spawn
    /// two concurrent loops that both call `apply(_:)` on every frame,
    /// doubling every UI update.
    func startStreaming() {
        streamTask?.cancel()
        let task = Task { [weak self] in
            guard let self else { return }
            await self.streamLoop()
        }
        streamTask = task
    }

    /// One-off fallback fetch. Used two ways:
    ///   (1) As the bootstrap step at the top of every stream connect,
    ///       so the app has data before the first SSE frame arrives.
    ///   (2) As a defensive hedge after any write from AppState.
    /// A failure here is non-fatal — the stream will still take over.
    func refreshNow() async {
        do {
            let state = try await apiService.fetchDashboardState()
            apply(state)
        } catch {
            // Only surface as an error if we have no data yet at all —
            // otherwise the stream is the primary path and a hedge
            // fetch failing is fine to swallow silently.
            if staffPositions.isEmpty {
                errorMessage = humanReadable(error)
                isLoading = false
            }
        }
    }

    private func streamLoop() async {
        var reconnectDelay = reconnectDelayNanoseconds
        while !Task.isCancelled {
            // Bootstrap: fetch the current snapshot BEFORE opening the
            // long-lived stream, so the UI never sits empty waiting on
            // the first SSE frame (which on a cold Render dyno can take
            // several seconds). If this fails we still try to open the
            // stream — SSE is the primary path, this is just the
            // fast-start.
            await refreshNow()

            do {
                try await connectAndConsume()
                reconnectDelay = reconnectDelayNanoseconds
            } catch {
                if Task.isCancelled { return }
                isStreamConnected = false
                errorMessage = humanReadable(error)
            }
            if Task.isCancelled { return }
            let jitter = UInt64.random(in: 0...(reconnectDelay / 4))
            try? await Task.sleep(nanoseconds: reconnectDelay + jitter)
            reconnectDelay = min(reconnectDelay * 2, maximumReconnectDelayNanoseconds)
        }
    }

    private func connectAndConsume() async throws {
        let (bytes, response) = try await apiService.openDashboardStream()
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }

        isStreamConnected = true
        isLoading = false
        errorMessage = nil

        var dataBuffer = ""
        for try await line in bytes.lines {
            if Task.isCancelled { return }

            if line.isEmpty {
                // Blank line = SSE event boundary; dispatch what we've
                // accumulated for this event.
                if !dataBuffer.isEmpty {
                    apply(payload: dataBuffer)
                    dataBuffer = ""
                }
                continue
            }
            if line.hasPrefix(":") { continue } // SSE comment/keep-alive
            if line.hasPrefix("data:") {
                let value = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
                // SSE allows multiple `data:` lines per event, joined by
                // newlines. For our JSON payloads the backend always
                // sends one, but handling the general case correctly is
                // cheap insurance if that ever changes.
                if dataBuffer.isEmpty {
                    dataBuffer = value
                } else {
                    dataBuffer += "\n" + value
                }
            }
        }

        // The for-await loop only exits when the server closes the
        // connection — treat that the same as any other disconnect, so
        // the outer streamLoop backs off and reconnects.
        isStreamConnected = false
        throw URLError(.networkConnectionLost)
    }

    private func apply(payload: String) {
        guard let data = payload.data(using: .utf8) else { return }
        do {
            let state = try JSONDecoder().decode(DashboardStateResponse.self, from: data)
            apply(state)
        } catch {
            // A decode failure means the backend contract drifted — the
            // right response is to keep the last known good state and
            // surface a distinguishable error (rather than crashing or
            // silently clearing the UI). This tends to be the fastest
            // way to notice a backend model changed and iOS didn't.
            errorMessage = "Failed to decode live update: \(error.localizedDescription)"
            #if DEBUG
            let preview = payload.prefix(300)
            print("[VitalTime] SSE decode failure: \(error)\nPayload prefix: \(preview)")
            #endif
        }
    }

    private func apply(_ state: DashboardStateResponse) {
        // Reject only genuinely out-of-order snapshots (older than one
        // already rendered) — everything else is trusted as-is. This is
        // ordering-only protection between this device's own concurrent
        // requests; it says nothing about cross-device consistency.
        if state.server_time < lastServerTime {
            return
        }
        lastServerTime = state.server_time

        staffPositions = state.staff
        onEventsReceived?(state.events.map(ActiveEvent.init(from:)))
        isLoading = false
        errorMessage = nil
    }

    private func humanReadable(_ error: Error) -> String {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .networkConnectionLost:
                return "Live connection dropped — reconnecting…"
            case .notConnectedToInternet:
                return "No internet — reconnecting when back online…"
            case .timedOut:
                return "Server didn't respond — retrying…"
            default:
                break
            }
        }
        return error.localizedDescription
    }

    /// SSE connections don't survive iOS suspending the app in the
    /// background — the socket gets torn down while backgrounded.
    /// Restart the stream as soon as the app becomes active again,
    /// rather than waiting out the normal reconnect backoff. Guarded
    /// so a spurious didBecomeActive (e.g. after a modal sheet
    /// dismisses on some iOS versions) while already connected doesn't
    /// gratuitously tear down a healthy stream.
    private func observeAppLifecycle() {
        let foregroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if !self.isStreamConnected {
                    self.startStreaming()
                }
            }
        }
        let backgroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didEnterBackgroundNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.streamTask?.cancel()
                self?.isStreamConnected = false
            }
        }
        lifecycleObservers = [foregroundObserver, backgroundObserver]
    }
}

#Preview {
    ContentView()
}
