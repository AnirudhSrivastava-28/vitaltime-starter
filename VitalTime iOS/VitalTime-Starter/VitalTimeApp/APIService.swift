import Foundation

final class APIService {
    static let defaultBaseURL = "https://vitaltime.onrender.com"

    private let baseURLString: String
    private let session: URLSession
    /// Separate session for SSE. Shared URLSession has a default
    /// `timeoutIntervalForResource` of 7 days and applies aggressive
    /// response caching — both wrong for a long-lived streaming
    /// connection where the same URL keeps returning "fresh" data
    /// forever. This session disables the URL cache entirely,
    /// disables `waitsForConnectivity` (we want fast failure to
    /// trigger reconnect, not silent waiting), and sets an explicit
    /// long resource timeout so the stream can outlive Render's
    /// idle-connection quirks.
    private let streamSession: URLSession

    init(baseURLString: String? = nil, session: URLSession = .shared) {
        let envOverride = ProcessInfo.processInfo.environment["VITALTIME_BASE_URL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedOverride = (envOverride?.isEmpty == true) ? nil : envOverride
        self.baseURLString = baseURLString ?? resolvedOverride ?? Self.defaultBaseURL
        self.session = session

        let streamConfig = URLSessionConfiguration.default
        streamConfig.timeoutIntervalForRequest = 3600
        streamConfig.timeoutIntervalForResource = 86400
        streamConfig.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        streamConfig.urlCache = nil
        streamConfig.waitsForConnectivity = false
        streamConfig.httpAdditionalHeaders = [
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        ]
        self.streamSession = URLSession(configuration: streamConfig)
    }

    func routeEvent(room: String, symptomTags: [String], submittedAt: String) async throws -> RouteEventResponse {
        let requestBody = RouteEventRequest(room: room, symptomTags: symptomTags, submittedAt: submittedAt)
        let request = try makeRequest(path: "/route-event", method: "POST", body: requestBody)
        return try await perform(request, as: RouteEventResponse.self)
    }

    func fetchStaffPositions() async throws -> [StaffPosition] {
        let request = try makeRequest(path: "/staff-positions", method: "GET")
        return try await perform(request, as: [StaffPosition].self)
    }

    /// One-off atomic snapshot of staff + events.
    ///
    /// Used as a bootstrap on every SSE connect: /dashboard-stream's
    /// first frame can be slow to arrive (Render cold start, TLS
    /// handshake, first tick of the SSE loop), and until it does the
    /// app has no data at all. Bootstrapping with this single HTTP GET
    /// gets the UI populated immediately, then the stream takes over
    /// for updates. Also called after a submit as a defensive hedge
    /// against a stream that's connected but hasn't yet been signalled.
    func fetchDashboardState() async throws -> DashboardStateResponse {
        let request = try makeRequest(path: "/dashboard-state", method: "GET")
        return try await perform(request, as: DashboardStateResponse.self)
    }

    /// Opens a persistent Server-Sent Events connection to
    /// /dashboard-stream. Returns the raw byte stream + response so the
    /// caller (LivePositionsModel) can read it line-by-line and decode
    /// each "data: ..." event as it arrives — this call itself returns as
    /// soon as the connection is established, not when it closes.
    ///
    /// Uses `streamSession` (not the shared session) so cache policy and
    /// timeouts are appropriate for a long-lived streaming connection.
    func openDashboardStream() async throws -> (URLSession.AsyncBytes, URLResponse) {
        let request = try makeStreamRequest(path: "/dashboard-stream")
        return try await streamSession.bytes(for: request)
    }

    func clearAssignment(eventId: String) async throws -> ClearAssignmentResponse {
        let requestBody = ClearAssignmentRequest(eventId: eventId)
        let request = try makeRequest(path: "/clear-assignment", method: "POST", body: requestBody)
        return try await perform(request, as: ClearAssignmentResponse.self)
    }

    func resetSimulation() async throws -> ResetSimulationResponse {
        let request = try makeRequest(path: "/reset-simulation", method: "POST")
        return try await perform(request, as: ResetSimulationResponse.self)
    }

    private func makeRequest<T: Encodable>(path: String, method: String, body: T? = nil) throws -> URLRequest {
        guard let url = URL(string: baseURLString + path) else {
            throw URLError(.badURL)
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData

        if let body {
            request.httpBody = try JSONEncoder().encode(body)
        }

        return request
    }

    private func makeRequest(path: String, method: String) throws -> URLRequest {
        guard let url = URL(string: baseURLString + path) else {
            throw URLError(.badURL)
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        return request
    }

    private func makeStreamRequest(path: String) throws -> URLRequest {
        guard let url = URL(string: baseURLString + path) else {
            throw URLError(.badURL)
        }

        var request = URLRequest(url: url)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        // Long-lived by design — the backend's own SSE tick (~1s) acts as
        // a keep-alive, so this is a ceiling far above any expected gap
        // between server-sent chunks, not a real per-request budget.
        request.timeoutInterval = 3600
        return request
    }

    private func perform<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw APIError.httpStatus(code: httpResponse.statusCode, message: body)
        }

        let decoder = JSONDecoder()
        return try decoder.decode(T.self, from: data)
    }
}

private struct RouteEventRequest: Encodable {
    let room: String
    let symptom_tags: [String]
    let submitted_at: String

    init(room: String, symptomTags: [String], submittedAt: String) {
        self.room = room
        self.symptom_tags = symptomTags
        self.submitted_at = submittedAt
    }
}

private struct ClearAssignmentRequest: Encodable {
    let event_id: String

    init(eventId: String) {
        self.event_id = eventId
    }
}

enum APIError: LocalizedError {
    case httpStatus(code: Int, message: String)

    var errorDescription: String? {
        switch self {
        case .httpStatus(let code, let message):
            return "Request failed with status \(code): \(message)"
        }
    }
}
