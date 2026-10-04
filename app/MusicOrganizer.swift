// Music Organizer: a panel that pops over Music from its Scripts menu (✨ Organizer).
//
// The panel reads what's open in Music (playlist, selected songs) and offers what fits. Every change goes through the Python
// organizer (`organizer.py api ...`): plans, verification after each step, audit log
// and undo. The panel only shows and applies what you tick.

import AppKit
import SwiftUI

// MARK: - Backend --------------------------------------------------------------------

struct Ctx: Decodable { let scannedAt: String?; let playlist: PL?; let selection: [Sel] }
struct PL: Decodable { let id, name: String; let editable: Bool; let size, labelled: Int; let canSplit: Bool }
struct Sel: Decodable, Identifiable { let id, name, artist: String; let scanned: Bool }

struct TrackNote: Decodable, Hashable { let name, artist: String; let note: String? }
struct Waiting: Decodable, Hashable, Identifiable { var id: String { name + artist }; let name, artist: String; let url: String? }
struct OpView: Decodable, Identifiable, Hashable {
    var id: Int { n }
    let n: Int; let op, status, text: String; let approved: Bool
    let reason, ref, playlist, name, bucket, result: String?
    let nameOptions: [String]; let count: Int; let tracks: [TrackNote]
}
struct PlanView: Decodable, Identifiable, Hashable {
    let id, title, createdAt: String; let pending: Int; let isUndo: Bool
    let ops: [OpView]; let waiting: [Waiting]?
}
struct PlansResp: Decodable { let plans: [PlanView] }
struct PlanResp: Decodable { let plan: PlanView }
struct ResultRow: Decodable, Hashable, Identifiable { var id: Int { n }; let n: Int; let status, text: String; let detail: String? }
struct ApplyResp: Decodable, Hashable { let plan: String?; let applied, total: Int?; let results: [ResultRow]?; let undo: String?; let waiting: [Waiting]? }
struct Suggestion: Decodable, Hashable { let playlistId, playlist, why: String; let score: Double }
struct BelongSong: Decodable, Identifiable { let id, name, artist: String; let suggestions: [Suggestion] }
struct BelongResp: Decodable { let songs: [BelongSong]; let noMatch: [Sel2] }
struct Sel2: Decodable, Identifiable { let id, name, artist: String }
struct ArtistPick: Decodable, Identifiable { var id: Int { i }; let i: Int; let name, artist, why: String; let owned: Bool; let url: String? }
struct ArtistResp: Decodable { let artist: String; let soundUsed: Bool; let picks: [ArtistPick] }
struct DiscoverPick: Decodable, Identifiable { var id: String { name + artist }; let name, artist: String; let url, chart: String?; let fans: Int?; let because: [String] }
struct DiscoverResp: Decodable { let picks: [DiscoverPick] }
struct MoveTarget: Decodable, Identifiable { let id, name, group: String }
struct MoveGroup: Decodable, Identifiable { var id: String { home.id }; let home: Sel2Home; let source: String; let tracks: [Sel2]; let targets: [MoveTarget] }
struct Sel2Home: Decodable { let id, name: String }
struct MoveResp: Decodable { let groups: [MoveGroup] }
struct DescribePick: Decodable, Identifiable { var id: Int { i }; let i: Int; let name, artist, why: String; let year: Int? }
struct DescribeResp: Decodable { let text: String; let chips: [String]; let picks: [DescribePick] }
struct PlaylistRow: Decodable, Identifiable { let id, name: String; let size, labelled: Int; let canSplit: Bool }
struct PlaylistsResp: Decodable { let playlists: [PlaylistRow] }
struct RefreshResp: Decodable, Hashable { let tracks, new, unlabelled: Int }
struct ErrorBox: Decodable { let error: String? }

/// Output collected from the organizer process on background threads.
final class OutputBuffer: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()
    func append(_ d: Data) { lock.lock(); data.append(d); lock.unlock() }
    func lastLine() -> Data {
        lock.lock(); defer { lock.unlock() }
        return data.split(separator: UInt8(ascii: "\n")).last.map { Data($0) } ?? Data()
    }
}

struct BackendError: LocalizedError { let message: String; var errorDescription: String? { message } }

enum Backend {
    static let info = Bundle.main.infoDictionary ?? [:]
    static let project = info["OrganizerProject"] as? String ?? FileManager.default.currentDirectoryPath
    static let python = info["OrganizerPython"] as? String ?? "/usr/bin/python3"

    /// Runs `organizer.py api <command> <args>`, streaming PROGRESS lines, and decodes the JSON answer.
    static func call<T: Decodable>(_ type: T.Type, _ command: String, _ args: [String] = [], body: [String: Any]? = nil,
                                   progress: @escaping (String) -> Void = { _ in }) async throws -> T {
        let data: Data = try await withCheckedThrowingContinuation { cont in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = ["organizer.py", "api", command] + args
            p.currentDirectoryURL = URL(fileURLWithPath: project)
            let out = Pipe(), err = Pipe(), inp = Pipe()
            p.standardOutput = out; p.standardError = err; p.standardInput = inp
            let collected = OutputBuffer()
            out.fileHandleForReading.readabilityHandler = { h in collected.append(h.availableData) }
            err.fileHandleForReading.readabilityHandler = { h in
                guard let s = String(data: h.availableData, encoding: .utf8) else { return }
                for line in s.split(separator: "\n") where line.hasPrefix("PROGRESS ") {
                    let text = String(line.dropFirst(9))
                    DispatchQueue.main.async { progress(text) }
                }
            }
            p.terminationHandler = { _ in
                out.fileHandleForReading.readabilityHandler = nil
                err.fileHandleForReading.readabilityHandler = nil
                collected.append(out.fileHandleForReading.readDataToEndOfFile())
                cont.resume(returning: collected.lastLine())
            }
            do {
                try p.run()
                if let body, let json = try? JSONSerialization.data(withJSONObject: body) {
                    inp.fileHandleForWriting.write(json)
                }
                try? inp.fileHandleForWriting.close()
            } catch {
                cont.resume(throwing: BackendError(message: "Couldn't start the organizer: \(error.localizedDescription)"))
            }
        }
        if let e = try? JSONDecoder().decode(ErrorBox.self, from: data), let msg = e.error {
            throw BackendError(message: msg)
        }
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw BackendError(message: "Unexpected answer from the organizer.") }
    }
}

// MARK: - Model ----------------------------------------------------------------------

enum Screen: Equatable {
    case home, plans, plan(PlanView), belong, artist, discover, move, result(ApplyResp, String), refresh(RefreshResp?)
    case pickPlaylist, describe
}

@MainActor final class AppModel: ObservableObject {
    @Published var screen: Screen = .home
    @Published var back: [Screen] = []
    @Published var ctx: Ctx?
    @Published var busy: String?
    @Published var error: String?
    @Published var pendingCount = 0
    @Published var popToken = 0
    @Published var panelOpen = false
    @Published var pendingDescribe: String?  // from musicorganizer://describe?q=...

    func go(_ s: Screen) {
        withAnimation(.spring(response: 0.42, dampingFraction: 0.84)) { back.append(screen); screen = s }
    }
    func goBack() {
        withAnimation(.spring(response: 0.42, dampingFraction: 0.84)) { screen = back.popLast() ?? .home }
    }
    func home() {
        withAnimation(.spring(response: 0.42, dampingFraction: 0.84)) { back = []; screen = .home }
        Task { await loadContext() }
    }

    func run<T: Decodable>(_ t: T.Type, _ label: String, _ cmd: String, _ args: [String] = [],
                           body: [String: Any]? = nil) async -> T? {
        withAnimation(.easeOut(duration: 0.2)) { busy = label; error = nil }
        defer { withAnimation(.easeOut(duration: 0.25)) { busy = nil } }
        do {
            return try await Backend.call(t, cmd, args, body: body) { [weak self] p in
                withAnimation(.easeInOut(duration: 0.2)) { self?.busy = p }
            }
        } catch {
            withAnimation(.spring()) { self.error = error.localizedDescription }
            return nil
        }
    }

    func loadContext() async {
        if let c = try? await Backend.call(Ctx.self, "context") { withAnimation(.spring()) { ctx = c } }
        if let p = try? await Backend.call(PlansResp.self, "plans") {
            withAnimation(.spring()) { pendingCount = p.plans.filter { !$0.isUndo }.count }
        }
    }
}

// MARK: - Style ----------------------------------------------------------------------

let brand = LinearGradient(colors: [Color(red: 1, green: 0.22, blue: 0.42), Color(red: 0.6, green: 0.27, blue: 1)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)

struct CardStyle: ViewModifier {
    var highlighted = false
    @State private var hover = false
    func body(content: Content) -> some View {
        content
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(.white.opacity(hover ? 0.12 : 0.07)))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(highlighted || hover ? AnyShapeStyle(brand) : AnyShapeStyle(.white.opacity(0.08)), lineWidth: 1.2))
            .scaleEffect(hover ? 1.025 : 1)
            .shadow(color: .black.opacity(hover ? 0.25 : 0), radius: 10, y: 4)
            .animation(.spring(response: 0.3, dampingFraction: 0.7), value: hover)
            .onHover { hover = $0 }
    }
}
extension View { func card(_ highlighted: Bool = false) -> some View { modifier(CardStyle(highlighted: highlighted)) } }

struct BrandButton: View {
    let title: String; var icon = "sparkles"; var enabled = true; let action: () -> Void
    @State private var pressed = false
    var body: some View {
        Button {
            withAnimation(.spring(response: 0.25, dampingFraction: 0.5)) { pressed = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { pressed = false; action() }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: icon).symbolEffect(.bounce, value: pressed)
                Text(title).fontWeight(.semibold)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 11)
            .background(Capsule().fill(enabled ? AnyShapeStyle(brand) : AnyShapeStyle(.gray.opacity(0.3))))
            .foregroundStyle(.white)
            .scaleEffect(pressed ? 0.94 : 1)
        }
        .buttonStyle(.plain).disabled(!enabled)
    }
}

struct Check: View {
    let on: Bool
    var body: some View {
        ZStack {
            Circle().strokeBorder(on ? AnyShapeStyle(brand) : AnyShapeStyle(.secondary), lineWidth: 1.5)
            if on { Circle().fill(brand).padding(3).transition(.scale.combined(with: .opacity)) }
            if on { Image(systemName: "checkmark").font(.system(size: 9, weight: .heavy)).foregroundStyle(.white) }
        }
        .frame(width: 20, height: 20)
        .animation(.spring(response: 0.3, dampingFraction: 0.6), value: on)
    }
}

struct Header: View {
    let title: String; var subtitle: String? = nil; var showBack = true
    @EnvironmentObject var model: AppModel
    var body: some View {
        HStack(spacing: 10) {
            if showBack {
                Button { model.goBack() } label: {
                    Image(systemName: "chevron.left").font(.system(size: 13, weight: .bold))
                        .frame(width: 28, height: 28).background(Circle().fill(.white.opacity(0.1)))
                }.buttonStyle(.plain)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 17, weight: .bold, design: .rounded)).lineLimit(1)
                if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer()
        }
    }
}

// MARK: - Root -----------------------------------------------------------------------

struct RootView: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        ZStack {
            Group {
                switch model.screen {
                case .home: HomeView()
                case .plans: PlansView()
                case .plan(let p): PlanDetailView(plan: p)
                case .belong: BelongView()
                case .artist: ArtistView()
                case .discover: DiscoverView()
                case .move: MoveView()
                case .result(let r, let title): ResultView(result: r, title: title)
                case .refresh(let r): RefreshView(result: r)
                case .pickPlaylist: PickPlaylistView()
                case .describe: DescribeView()
                }
            }
            .padding(16)
            .transition(.asymmetric(insertion: .move(edge: .trailing).combined(with: .opacity),
                                    removal: .move(edge: .leading).combined(with: .opacity)))
            .id(screenKey)

            if let busy = model.busy { BusyOverlay(text: busy).transition(.opacity.combined(with: .scale(scale: 1.05))) }
            if let err = model.error {
                VStack {
                    Spacer()
                    HStack(alignment: .top) {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.yellow)
                        Text(err).font(.callout).fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        Button { withAnimation { model.error = nil } } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                    }
                    .padding(12).background(RoundedRectangle(cornerRadius: 14).fill(.black.opacity(0.75)))
                    .padding(12)
                }.transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .frame(minWidth: 400, maxWidth: 400, minHeight: 420, maxHeight: .infinity)
        .onAppear { Task { await model.loadContext() } }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didBecomeKeyNotification)) { _ in
            if model.screen == .home { Task { await model.loadContext() } }
        }
    }
    var screenKey: String {
        switch model.screen {
        case .home: "home"; case .plans: "plans"; case .plan(let p): "plan-\(p.id)"; case .belong: "belong"
        case .artist: "artist"; case .discover: "discover"; case .move: "move"; case .result(let r, _): "result-\(r.plan ?? "")"
        case .refresh: "refresh"; case .pickPlaylist: "pick"; case .describe: "describe"
        }
    }
}

struct BusyOverlay: View {
    let text: String
    @State private var spin = false
    @State private var pulse = false
    var body: some View {
        ZStack {
            Rectangle().fill(.ultraThinMaterial).ignoresSafeArea()
            VStack(spacing: 18) {
                ZStack {
                    Circle().stroke(.white.opacity(0.08), lineWidth: 6)
                    Circle().trim(from: 0, to: 0.72)
                        .stroke(AngularGradient(colors: [.pink, .purple, .pink], center: .center),
                                style: StrokeStyle(lineWidth: 6, lineCap: .round))
                        .rotationEffect(.degrees(spin ? 360 : 0))
                    Image(systemName: "music.note").font(.system(size: 26, weight: .bold)).foregroundStyle(brand)
                        .scaleEffect(pulse ? 1.15 : 0.9)
                }
                .frame(width: 72, height: 72)
                Text(text).font(.callout.weight(.medium)).multilineTextAlignment(.center)
                    .contentTransition(.opacity).padding(.horizontal, 30).id(text)
            }
        }
        .onAppear {
            withAnimation(.linear(duration: 1).repeatForever(autoreverses: false)) { spin = true }
            withAnimation(.easeInOut(duration: 0.6).repeatForever()) { pulse = true }
        }
    }
}

// MARK: - Home -----------------------------------------------------------------------

struct ActionCard: View {
    let icon: String; let title: String; let subtitle: String; var badge: Int = 0; var hero = false
    let action: () -> Void
    @State private var tapped = false
    var body: some View {
        Button {
            tapped.toggle()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { action() }
        } label: {
            HStack(spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 11, style: .continuous).fill(hero ? AnyShapeStyle(brand) : AnyShapeStyle(.white.opacity(0.1)))
                    Image(systemName: icon).font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(hero ? AnyShapeStyle(.white) : AnyShapeStyle(brand))
                        .symbolEffect(.bounce, value: tapped)
                }
                .frame(width: 40, height: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer(minLength: 0)
                if badge > 0 {
                    Text("\(badge)").font(.caption.bold()).padding(.horizontal, 7).padding(.vertical, 3)
                        .background(Capsule().fill(brand)).foregroundStyle(.white)
                        .contentTransition(.numericText())
                }
                Image(systemName: "chevron.right").font(.caption.bold()).foregroundStyle(.tertiary)
            }
            .contentShape(Rectangle())
            .card(hero)
        }
        .buttonStyle(.plain)
    }
}

struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @State private var shown = false
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Organizer").font(.system(size: 26, weight: .heavy, design: .rounded)).foregroundStyle(brand)
                    Text(contextLine).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        .contentTransition(.opacity)
                }
                Spacer()
                Button { Task { await model.loadContext() } } label: {
                    Image(systemName: "arrow.clockwise").frame(width: 28, height: 28).background(Circle().fill(.white.opacity(0.08)))
                }.buttonStyle(.plain).help("Read Music again")
                Button { FloatingPanel.quit() } label: {
                    Image(systemName: "power").frame(width: 28, height: 28).background(Circle().fill(.white.opacity(0.08)))
                }.buttonStyle(.plain).help("Quit Organizer completely (the ✨ button comes back at your next login)")
            }
            ScrollView(showsIndicators: false) {
                VStack(spacing: 10) {
                    ForEach(Array(cards.enumerated()), id: \.offset) { i, c in
                        c.opacity(shown ? 1 : 0).offset(y: shown ? 0 : 18)
                            .animation(.spring(response: 0.5, dampingFraction: 0.8).delay(Double(i) * 0.05), value: shown)
                    }
                }.padding(.vertical, 2)
            }
            Spacer(minLength: 0)
            Text("Nothing changes in Music until you tick it and press Apply. Every change can be undone. Esc or ✨ to close.")
                .font(.caption2).foregroundStyle(.tertiary)
        }
        .onAppear { shown = true }
    }

    var contextLine: String {
        guard let c = model.ctx else { return "Reading Music…" }
        var parts: [String] = []
        if let p = c.playlist { parts.append("Viewing \(p.name)") }
        if !c.selection.isEmpty { parts.append("\(c.selection.count) selected") }
        return parts.isEmpty ? "Open a playlist or select songs in Music" : parts.joined(separator: " · ")
    }

    var cards: [AnyView] {
        var out: [AnyView] = []
        let c = model.ctx
        if let p = c?.playlist, p.canSplit {
            out.append(AnyView(ActionCard(icon: "square.split.2x2.fill", title: "Split “\(p.name)”",
                                          subtitle: "\(p.size) songs, grouped by sound and energy. The original stays.") {
                Task {
                    if let r = await model.run(PlanResp.self, "Sorting \(p.name) by vibe…", "split", [p.id]) { model.go(.plan(r.plan)) }
                }
            }))
        }
        let sel = c?.selection ?? []
        if !sel.isEmpty {
            let what = sel.count == 1 ? "“\(sel[0].name)”" : "these \(sel.count) songs"
            out.append(AnyView(ActionCard(icon: "rectangle.stack.badge.plus", title: "Where does \(what) belong?",
                                          subtitle: "Playlists that fit, from songs that sound and feel alike") {
                model.go(.belong)
            }))
            out.append(AnyView(ActionCard(icon: "sparkle.magnifyingglass", title: "Discover songs like \(sel.count == 1 ? "this" : "these")",
                                          subtitle: "Lesser-known songs charting right now") { model.go(.discover) }))
            out.append(AnyView(ActionCard(icon: "arrow.left.arrow.right", title: "Move to another split playlist",
                                          subtitle: "For songs in a playlist the organizer made") { model.go(.move) }))
        }
        out.insert(AnyView(ActionCard(icon: "text.bubble.fill", title: "Describe a playlist",
                                      subtitle: "Say what you want to hear; I'll pick the songs for you to check",
                                      hero: true) { model.go(.describe) }), at: 0)
        out.append(AnyView(ActionCard(icon: "square.split.2x2", title: "Split a playlist",
                                      subtitle: "By sound first, then energy. You check every song first") {
            model.go(.pickPlaylist)
        }))
        out.append(AnyView(ActionCard(icon: "checklist", title: "Review & apply changes",
                                      subtitle: "Pick changes, name new playlists, apply", badge: model.pendingCount) { model.go(.plans) }))
        out.append(AnyView(ActionCard(icon: "person.wave.2.fill", title: "Artist playlist",
                                      subtitle: "Type an artist, get their best songs for your taste") { model.go(.artist) }))
        out.append(AnyView(ActionCard(icon: "arrow.triangle.2.circlepath", title: "Refresh library",
                                      subtitle: "Rescan Music and fetch metadata for new songs") {
            Task {
                model.go(.refresh(nil))
                if let r = await model.run(RefreshResp.self, "Re-reading your library…", "refresh") {
                    model.screen = .refresh(r)
                }
            }
        }))
        if sel.isEmpty && c?.playlist?.canSplit != true {
            out.insert(AnyView(HStack(spacing: 8) {
                Image(systemName: "hand.point.up.left.fill").foregroundStyle(brand)
                Text("Tip: select songs in Music to see where they belong.")
                    .font(.caption).foregroundStyle(.secondary)
            }.card()), at: 0)
        }
        return out
    }
}

// MARK: - Plans ------------------------------------------------------------------------

struct PlansView: View {
    @EnvironmentObject var model: AppModel
    @State private var plans: [PlanView] = []
    @State private var loaded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Review & apply", subtitle: "Plans waiting for your OK")
            if loaded && plans.isEmpty {
                Spacer()
                VStack(spacing: 8) {
                    Image(systemName: "checkmark.seal.fill").font(.system(size: 40)).foregroundStyle(brand)
                    Text("Nothing waiting").font(.headline)
                    Text("Split a playlist or ask where songs belong to get suggestions.").font(.caption).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity)
                Spacer()
            }
            ScrollView(showsIndicators: false) {
                VStack(spacing: 10) {
                    ForEach(Array(plans.enumerated()), id: \.element.id) { i, p in
                        Button { model.go(.plan(p)) } label: {
                            HStack(spacing: 12) {
                                Image(systemName: p.isUndo ? "arrow.uturn.backward.circle.fill" : "list.bullet.rectangle.portrait.fill")
                                    .font(.title2).foregroundStyle(brand)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(p.title).font(.system(size: 14, weight: .semibold)).lineLimit(2)
                                    Text("\(p.pending) changes · \(p.createdAt.prefix(10))").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Image(systemName: "chevron.right").font(.caption.bold()).foregroundStyle(.tertiary)
                            }.contentShape(Rectangle()).card()
                        }
                        .buttonStyle(.plain)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .animation(.spring().delay(Double(i) * 0.04), value: plans.count)
                    }
                }
            }
        }
        .task {
            if let r = await model.run(PlansResp.self, "Loading plans…", "plans") {
                withAnimation(.spring()) { plans = r.plans; loaded = true }
            }
        }
    }
}

struct PlanDetailView: View {
    let plan: PlanView
    @EnvironmentObject var model: AppModel
    @State private var chosen: Set<Int> = []
    @State private var names: [Int: String] = [:]
    @State private var expanded: Set<Int> = []
    @State private var editing: Int?

    var pending: [OpView] { plan.ops.filter { $0.status == "pending" } }
    var rows: [OpView] { pending.filter { $0.op != "create_playlist" } }
    func create(for op: OpView) -> OpView? { pending.first { $0.op == "create_playlist" && $0.ref != nil && $0.ref == op.ref } }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: plan.title, subtitle: "Tick what you want. New playlists can be renamed.")
            HStack {
                Button(chosen.count == rows.count ? "Untick all" : "Tick all") {
                    withAnimation(.spring()) { chosen = chosen.count == rows.count ? [] : Set(rows.map(\.n)) }
                }.buttonStyle(.plain).font(.caption.bold()).foregroundStyle(brand)
                Spacer()
                Text("\(chosen.count) of \(rows.count) selected").font(.caption).foregroundStyle(.secondary)
                    .contentTransition(.numericText())
            }
            ScrollView(showsIndicators: false) {
                VStack(spacing: 10) {
                    ForEach(rows) { op in row(op) }
                    if !(plan.waiting ?? []).isEmpty {
                        Text("\((plan.waiting ?? []).count) more songs aren't in your library yet; you can add them after creating the playlist.")
                            .font(.caption).foregroundStyle(.secondary).card()
                    }
                }.padding(.vertical, 2)
            }
            BrandButton(title: chosen.isEmpty ? "Tick something to apply" : "Apply \(chosen.count) change\(chosen.count == 1 ? "" : "s")",
                        icon: "wand.and.stars", enabled: !chosen.isEmpty) { apply() }
        }
        .onAppear {
            if plan.isUndo { chosen = Set(rows.map(\.n)) }
            for op in pending where op.op == "create_playlist" { names[op.n] = op.name ?? "" }
        }
    }

    @ViewBuilder func row(_ op: OpView) -> some View {
        let on = chosen.contains(op.n)
        let cr = create(for: op)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                Button { withAnimation(.spring(response: 0.3)) { if on { chosen.remove(op.n) } else { chosen.insert(op.n) } } } label: { Check(on: on) }
                    .buttonStyle(.plain).padding(.top, 2)
                VStack(alignment: .leading, spacing: 4) {
                    if let cr {
                        HStack(spacing: 6) {
                            Image(systemName: "sparkles").foregroundStyle(brand).font(.caption)
                            if editing == cr.n {
                                TextField("Playlist name", text: Binding(get: { names[cr.n] ?? "" }, set: { names[cr.n] = $0 }))
                                    .textFieldStyle(.roundedBorder).onSubmit { withAnimation { editing = nil } }
                            } else {
                                Text(names[cr.n] ?? cr.name ?? "").font(.system(size: 14, weight: .bold)).lineLimit(1)
                            }
                            Menu {
                                ForEach(cr.nameOptions, id: \.self) { n in Button(n) { withAnimation(.spring()) { names[cr.n] = n } } }
                                Divider()
                                Button("Type my own…") { withAnimation { editing = cr.n } }
                            } label: { Image(systemName: "pencil.circle.fill").foregroundStyle(brand) }
                                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                        }
                        Text("New playlist · \(op.count) songs\(cr.bucket.map { " · \($0)" } ?? "")").font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text(op.text + (op.count > 0 ? " (\(op.count))" : "")).font(.system(size: 13, weight: .semibold))
                        if let r = op.reason { Text(r).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                    }
                }
                Spacer(minLength: 0)
                if op.count > 0 {
                    Button { withAnimation(.spring()) { if expanded.contains(op.n) { expanded.remove(op.n) } else { expanded.insert(op.n) } } } label: {
                        Image(systemName: "chevron.down").rotationEffect(.degrees(expanded.contains(op.n) ? 180 : 0))
                    }.buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            if expanded.contains(op.n) {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(op.tracks, id: \.self) { t in
                        HStack(spacing: 6) {
                            Text("♪").foregroundStyle(brand)
                            Text("\(t.name)").lineLimit(1)
                            Text("– \(t.artist)").foregroundStyle(.secondary).lineLimit(1)
                        }.font(.caption)
                    }
                    if op.count > op.tracks.count { Text("+ \(op.count - op.tracks.count) more").font(.caption2).foregroundStyle(.tertiary) }
                }
                .padding(.leading, 30)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .card(on)
    }

    func apply() {
        var nameBody: [String: String] = [:]
        for op in rows where chosen.contains(op.n) { if let cr = create(for: op) { nameBody[String(cr.n)] = names[cr.n] ?? cr.name ?? "" } }
        Task {
            let label = plan.isUndo ? "Undoing…" : "Applying to Music…"
            if let r = await model.run(ApplyResp.self, label, "apply", body: ["plan": plan.id, "ops": Array(chosen), "names": nameBody]) {
                model.go(.result(r, plan.isUndo ? "Undone" : "Done"))
            }
        }
    }
}

// MARK: - Describe a playlist -----------------------------------------------------------

struct Chip: View {
    let text: String; var filled = false
    var body: some View {
        Text(text).font(.caption.weight(.semibold)).lineLimit(1)
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(Capsule().fill(filled ? AnyShapeStyle(brand) : AnyShapeStyle(.white.opacity(0.1))))
            .foregroundStyle(filled ? AnyShapeStyle(.white) : AnyShapeStyle(.primary))
    }
}

/// Wrapping row of chips.
struct FlowRow: Layout {
    var spacing: CGFloat = 6
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 360
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > width, x > 0 { x = 0; y += rowH + spacing; rowH = 0 }
            x += s.width + spacing; rowH = max(rowH, s.height)
        }
        return CGSize(width: width, height: y + rowH)
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > bounds.maxX, x > bounds.minX { x = bounds.minX; y += rowH + spacing; rowH = 0 }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing; rowH = max(rowH, s.height)
        }
    }
}

struct DescribeView: View {
    @EnvironmentObject var model: AppModel
    @State private var text = ""
    @State private var result: DescribeResp?
    @State private var keep: Set<Int> = []
    @FocusState private var focused: Bool
    let examples = ["Hard gym rap, no slow songs", "Late night Telugu melodies", "Sad indie for 2am",
                    "2010s boyband pop to sing along", "Anirudh mass bangers", "Chill R&B, no Drake"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Describe a playlist", subtitle: "Sound, mood, artists, language, era…")
            HStack(alignment: .top) {
                Image(systemName: "text.bubble").foregroundStyle(brand).padding(.top, 2)
                TextField("e.g. hard gym rap like Kendrick, no slow songs", text: $text, axis: .vertical)
                    .textFieldStyle(.plain).lineLimit(1...3).focused($focused).onSubmit(go)
                if !text.isEmpty {
                    Button(action: go) { Image(systemName: "arrow.up.circle.fill").font(.title2).foregroundStyle(brand) }
                        .buttonStyle(.plain).transition(.scale.combined(with: .opacity))
                }
            }
            .card(focused)
            .animation(.spring(response: 0.3), value: text.isEmpty)

            if let r = result {
                FlowRow { ForEach(r.chips, id: \.self) { Chip(text: $0, filled: true) } }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                HStack {
                    Text(r.picks.isEmpty ? "Nothing in your library matches that yet." :
                         "\(keep.count) of \(r.picks.count) songs, tap to drop any")
                        .font(.caption).foregroundStyle(.secondary).contentTransition(.numericText())
                    Spacer()
                }
                ScrollView(showsIndicators: false) {
                    VStack(spacing: 4) {
                        ForEach(r.picks) { p in
                            Button { withAnimation(.spring(response: 0.3)) { if keep.contains(p.i) { keep.remove(p.i) } else { keep.insert(p.i) } } } label: {
                                HStack(spacing: 10) {
                                    Check(on: keep.contains(p.i))
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(p.name).font(.callout.weight(.semibold)).lineLimit(1)
                                            .strikethrough(!keep.contains(p.i), color: .secondary)
                                        Text("\(p.artist) · \(p.why)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                    Spacer(minLength: 0)
                                }
                                .opacity(keep.contains(p.i) ? 1 : 0.45)
                                .contentShape(Rectangle()).padding(.vertical, 4).padding(.horizontal, 6)
                            }.buttonStyle(.plain)
                        }
                    }
                }
                BrandButton(title: "Create playlist with \(keep.count) songs", icon: "music.note.list", enabled: !keep.isEmpty) {
                    Task {
                        if let p = await model.run(PlanResp.self, "Preparing your playlist…", "describe-create", body: ["keep": Array(keep)]) {
                            model.go(.plan(p.plan))
                        }
                    }
                }
            } else {
                Text("Try one of these:").font(.caption).foregroundStyle(.secondary)
                FlowRow {
                    ForEach(examples, id: \.self) { e in
                        Button { text = e; go() } label: { Chip(text: e) }.buttonStyle(.plain)
                    }
                }
                Spacer()
            }
        }
        .onAppear {
            focused = true
            if let q = model.pendingDescribe { model.pendingDescribe = nil; text = q; go() }
        }
    }

    func go() {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        Task {
            if let r = await model.run(DescribeResp.self, "Picking songs for “\(t)”…", "describe", t.components(separatedBy: " ")) {
                withAnimation(.spring(response: 0.45, dampingFraction: 0.8)) { result = r; keep = Set(r.picks.map(\.i)) }
            }
        }
    }
}

// MARK: - Pick a playlist ------------------------------------------------------------

struct PickPlaylistView: View {
    @EnvironmentObject var model: AppModel
    @State private var rows: [PlaylistRow] = []
    @State private var query = ""
    var shown: [PlaylistRow] { query.isEmpty ? rows : rows.filter { $0.name.localizedCaseInsensitiveContains(query) } }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Split a playlist", subtitle: "Grouped by sound, then energy. The original stays.")
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search playlists", text: $query).textFieldStyle(.plain)
            }.card()
            ScrollView(showsIndicators: false) {
                VStack(spacing: 8) {
                    ForEach(Array(shown.enumerated()), id: \.element.id) { i, p in
                        Button {
                            Task {
                                if let r = await model.run(PlanResp.self, "Sorting \(p.name) by vibe…", "split", [p.id]) {
                                    model.go(.plan(r.plan))
                                }
                            }
                        } label: {
                            HStack(spacing: 12) {
                                ZStack {
                                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                                        .fill(p.canSplit ? AnyShapeStyle(brand) : AnyShapeStyle(.white.opacity(0.08)))
                                    Image(systemName: "music.note.list").foregroundStyle(.white)
                                }.frame(width: 36, height: 36)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(p.name).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                                    Text(p.canSplit ? "\(p.size) songs" : "\(p.size) songs · too small to split")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if p.canSplit { Image(systemName: "chevron.right").font(.caption.bold()).foregroundStyle(.tertiary) }
                            }.contentShape(Rectangle()).card()
                        }
                        .buttonStyle(.plain).disabled(!p.canSplit).opacity(p.canSplit ? 1 : 0.5)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .animation(.spring(response: 0.45, dampingFraction: 0.8).delay(Double(i) * 0.03), value: rows.count)
                    }
                }
            }
        }
        .task {
            if let r = await model.run(PlaylistsResp.self, "Loading your playlists…", "playlists") {
                withAnimation(.spring()) { rows = r.playlists }
            }
        }
    }
}

// MARK: - Result ---------------------------------------------------------------------

struct Confetti: View {
    @State private var burst = false
    let colors: [Color] = [.pink, .purple, .orange, .yellow, .mint, .blue]
    var body: some View {
        ZStack {
            ForEach(0..<18, id: \.self) { i in
                let angle = Double(i) / 18 * 2 * .pi
                Circle().fill(colors[i % colors.count]).frame(width: 7, height: 7)
                    .offset(x: burst ? cos(angle) * 80 : 0, y: burst ? sin(angle) * 80 : 0)
                    .opacity(burst ? 0 : 1)
                    .scaleEffect(burst ? 0.4 : 1)
            }
        }
        .onAppear { withAnimation(.easeOut(duration: 0.9).delay(0.25)) { burst = true } }
    }
}

struct ResultView: View {
    let result: ApplyResp; let title: String
    @EnvironmentObject var model: AppModel
    @State private var drawn = false
    @State private var tick = false
    var ok: Bool { (result.applied ?? 0) == (result.total ?? 0) }
    var body: some View {
        VStack(spacing: 14) {
            ZStack {
                if ok { Confetti() }
                Circle().trim(from: 0, to: drawn ? 1 : 0).stroke(ok ? AnyShapeStyle(brand) : AnyShapeStyle(.orange), style: StrokeStyle(lineWidth: 5, lineCap: .round))
                    .rotationEffect(.degrees(-90)).frame(width: 78, height: 78)
                Image(systemName: ok ? "checkmark" : "exclamationmark").font(.system(size: 32, weight: .heavy))
                    .foregroundStyle(ok ? AnyShapeStyle(brand) : AnyShapeStyle(.orange)).scaleEffect(tick ? 1 : 0.2).opacity(tick ? 1 : 0)
            }
            .frame(height: 110)
            .onAppear {
                withAnimation(.easeOut(duration: 0.5)) { drawn = true }
                withAnimation(.spring(response: 0.4, dampingFraction: 0.5).delay(0.35)) { tick = true }
            }
            Text(ok ? title : "Stopped partway").font(.system(size: 22, weight: .heavy, design: .rounded))
            Text("\(result.applied ?? 0) of \(result.total ?? 0) changes applied and checked in Music")
                .font(.callout).foregroundStyle(.secondary)
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(result.results ?? []) { r in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: r.status == "applied" ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundStyle(r.status == "applied" ? AnyShapeStyle(brand) : AnyShapeStyle(.orange))
                            VStack(alignment: .leading, spacing: 1) {
                                Text(r.text).font(.caption.weight(.semibold))
                                if let d = r.detail { Text(d).font(.caption2).foregroundStyle(.secondary) }
                            }
                        }
                    }
                    if let waiting = result.waiting, !waiting.isEmpty, let plan = result.plan {
                        WaitingList(waiting: waiting, plan: plan)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).card()
            }
            HStack(spacing: 10) {
                if let undo = result.undo {
                    Button {
                        Task { if let r = await model.run(ApplyResp.self, "Undoing…", "undo", [undo]) { model.go(.result(r, "Undone")) } }
                    } label: {
                        Label("Undo", systemImage: "arrow.uturn.backward").frame(maxWidth: .infinity).padding(.vertical, 10)
                            .background(Capsule().strokeBorder(brand, lineWidth: 1.5))
                    }.buttonStyle(.plain)
                }
                BrandButton(title: "Done", icon: "house.fill") { model.home() }
            }
        }
    }
}

struct WaitingList: View {
    let waiting: [Waiting]; let plan: String
    @EnvironmentObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider().padding(.vertical, 4)
            Text("Add these in Music with +, then fill the playlist:").font(.caption.bold())
            ForEach(waiting) { w in
                HStack {
                    Text(w.name).font(.caption).lineLimit(1)
                    Spacer()
                    if let u = w.url, let url = URL(string: u) {
                        Button("Open") { NSWorkspace.shared.open(url) }.buttonStyle(.plain).font(.caption.bold()).foregroundStyle(brand)
                    }
                }
            }
            Button {
                Task { if let r = await model.run(ApplyResp.self, "Looking for the songs you added…", "fill", [plan]) { model.go(.result(r, "Filled")) } }
            } label: { Label("I've added them – fill the playlist", systemImage: "tray.and.arrow.down.fill").font(.caption.bold()) }
                .buttonStyle(.plain).foregroundStyle(brand).padding(.top, 4)
        }
    }
}

// MARK: - Where do these belong? ------------------------------------------------------

struct BelongView: View {
    @EnvironmentObject var model: AppModel
    @State private var songs: [BelongSong] = []
    @State private var noMatch: [Sel2] = []
    @State private var picked: Set<String> = []  // "trackId|playlistId"
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Where do they belong?", subtitle: "Best match first; tap to choose")
            ScrollView(showsIndicators: false) {
                VStack(spacing: 10) {
                    ForEach(songs) { s in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(s.name).font(.system(size: 14, weight: .bold)).lineLimit(1)
                            Text(s.artist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            ForEach(Array(s.suggestions.enumerated()), id: \.offset) { i, sug in
                                let key = "\(s.id)|\(sug.playlistId)"
                                Button { withAnimation(.spring(response: 0.3)) { if picked.contains(key) { picked.remove(key) } else { picked.insert(key) } } } label: {
                                    HStack(alignment: .top, spacing: 8) {
                                        Check(on: picked.contains(key))
                                        VStack(alignment: .leading, spacing: 1) {
                                            HStack {
                                                Text(sug.playlist).font(.callout.weight(.semibold))
                                                if i == 0 { Text("BEST").font(.system(size: 9, weight: .heavy)).padding(.horizontal, 5).padding(.vertical, 2).background(Capsule().fill(brand)).foregroundStyle(.white) }
                                            }
                                            Text(sug.why).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                                        }
                                        Spacer(minLength: 0)
                                    }.contentShape(Rectangle())
                                }.buttonStyle(.plain)
                            }
                        }.card()
                    }
                    if !noMatch.isEmpty {
                        Text("No clear home for: " + noMatch.map(\.name).joined(separator: ", ")).font(.caption).foregroundStyle(.secondary).card()
                    }
                }
            }
            BrandButton(title: picked.isEmpty ? "Tap a playlist to choose it" : "Add to \(picked.count) playlist\(picked.count == 1 ? "" : "s")",
                        icon: "plus.circle.fill", enabled: !picked.isEmpty) {
                let picks = picked.map { $0.split(separator: "|").map(String.init) }
                Task { if let r = await model.run(ApplyResp.self, "Adding in Music…", "add", body: ["picks": picks]) { model.go(.result(r, "Added")) } }
            }
        }
        .task {
            let ids = model.ctx?.selection.map(\.id) ?? []
            if let r = await model.run(BelongResp.self, "Comparing with your playlists…", "belong", ids) {
                withAnimation(.spring()) {
                    songs = r.songs; noMatch = r.noMatch
                    picked = Set(r.songs.compactMap { s in s.suggestions.first.map { "\(s.id)|\($0.playlistId)" } })
                }
            }
        }
    }
}

// MARK: - Artist playlist --------------------------------------------------------------

struct ArtistView: View {
    @EnvironmentObject var model: AppModel
    @State private var name = ""
    @State private var result: ArtistResp?
    @State private var keep: Set<Int> = []
    @FocusState private var focused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Artist playlist", subtitle: "Their best songs for your taste")
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Artist name, e.g. Anirudh Ravichander", text: $name).textFieldStyle(.plain).focused($focused)
                    .onSubmit(search)
                if !name.isEmpty { Button("Go", action: search).buttonStyle(.plain).font(.callout.bold()).foregroundStyle(brand) }
            }.card(focused)
            if let r = result {
                Text("\(r.artist): \(r.picks.filter(\.owned).count) you have ✓, \(r.picks.filter { !$0.owned }.count) to add +")
                    .font(.caption).foregroundStyle(.secondary)
                ScrollView(showsIndicators: false) {
                    VStack(spacing: 6) {
                        ForEach(r.picks) { p in
                            Button { withAnimation(.spring(response: 0.3)) { if keep.contains(p.i) { keep.remove(p.i) } else { keep.insert(p.i) } } } label: {
                                HStack(spacing: 10) {
                                    Check(on: keep.contains(p.i))
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(p.name).font(.callout.weight(.semibold)).lineLimit(1)
                                        Text(p.owned ? "In your library" : "Not in your library yet").font(.caption2).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Text(p.owned ? "✓" : "+").font(.headline).foregroundStyle(brand)
                                }.contentShape(Rectangle()).padding(.vertical, 4).padding(.horizontal, 6)
                            }.buttonStyle(.plain)
                        }
                    }
                }
                BrandButton(title: "Create playlist with \(keep.count) songs", icon: "music.note.list", enabled: !keep.isEmpty) {
                    Task { if let p = await model.run(PlanResp.self, "Preparing the playlist…", "artist-create", body: ["keep": Array(keep)]) { model.go(.plan(p.plan)) } }
                }
            } else {
                Spacer()
            }
        }
        .onAppear { focused = true }
    }
    func search() {
        guard !name.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        Task {
            if let r = await model.run(ArtistResp.self, "Listening to \(name)'s catalogue… about a minute", "artist", [name]) {
                withAnimation(.spring()) { result = r; keep = Set(r.picks.map(\.i)) }
            }
        }
    }
}

// MARK: - Discover ---------------------------------------------------------------------

struct DiscoverView: View {
    @EnvironmentObject var model: AppModel
    @State private var picks: [DiscoverPick] = []
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Discover", subtitle: "Charting now, close to your selection")
            ScrollView(showsIndicators: false) {
                VStack(spacing: 8) {
                    ForEach(Array(picks.enumerated()), id: \.element.id) { i, p in
                        HStack(spacing: 10) {
                            Text("\(i + 1)").font(.system(size: 13, weight: .heavy, design: .rounded)).foregroundStyle(brand).frame(width: 22)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(p.name).font(.callout.weight(.semibold)).lineLimit(1)
                                Text(p.artist).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                if let c = p.chart { Text("📈 \(c)").font(.caption2).foregroundStyle(.secondary) }
                            }
                            Spacer()
                            if let u = p.url, let url = URL(string: u) {
                                Button { NSWorkspace.shared.open(url) } label: {
                                    Image(systemName: "play.circle.fill").font(.title2).foregroundStyle(brand)
                                }.buttonStyle(.plain).help("Open in Music")
                            }
                        }.card()
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                }
            }
        }
        .task {
            let ids = model.ctx?.selection.map(\.id) ?? []
            if let r = await model.run(DiscoverResp.self, "Checking what's charting near these songs… about a minute", "discover", ids) {
                withAnimation(.spring()) { picks = r.picks }
            }
        }
    }
}

// MARK: - Move ---------------------------------------------------------------------------

struct MoveView: View {
    @EnvironmentObject var model: AppModel
    @State private var groups: [MoveGroup] = []
    @State private var loaded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Header(title: "Move songs", subtitle: "Remembered for the next split")
            if loaded && groups.isEmpty {
                Text("Select songs inside a playlist the organizer made from a split, then come back here.")
                    .font(.callout).foregroundStyle(.secondary).card()
            }
            ScrollView(showsIndicators: false) {
                VStack(spacing: 10) {
                    ForEach(groups) { g in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(g.tracks.map(\.name).joined(separator: ", ")).font(.callout.weight(.semibold)).lineLimit(2)
                            Text("Now in \(g.home.name)").font(.caption).foregroundStyle(.secondary)
                            ForEach(g.targets) { t in
                                Button { move(g, t.id) } label: { Label(t.name, systemImage: "arrow.right.circle.fill").font(.callout) }
                                    .buttonStyle(.plain).foregroundStyle(brand)
                            }
                            Button { move(g, nil) } label: { Label("None – keep only in \(g.source)", systemImage: "minus.circle").font(.callout) }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                        }.card()
                    }
                }
            }
        }
        .task {
            let ids = model.ctx?.selection.map(\.id) ?? []
            if let r = await model.run(MoveResp.self, "Finding where these are…", "move-options", ids) {
                withAnimation(.spring()) { groups = r.groups; loaded = true }
            }
        }
    }
    func move(_ g: MoveGroup, _ target: String?) {
        var body: [String: Any] = ["home": g.home.id, "tracks": g.tracks.map(\.id)]
        if let target { body["target"] = target }
        Task { if let r = await model.run(ApplyResp.self, "Moving in Music…", "move", body: body) { model.go(.result(r, "Moved")) } }
    }
}

// MARK: - Refresh ------------------------------------------------------------------------

struct RefreshView: View {
    let result: RefreshResp?
    @EnvironmentObject var model: AppModel
    var body: some View {
        VStack(spacing: 14) {
            Header(title: "Refresh library")
            Spacer()
            if let r = result {
                Image(systemName: "checkmark.seal.fill").font(.system(size: 54)).foregroundStyle(brand).symbolEffect(.bounce, value: r.tracks)
                Text("\(r.tracks) songs scanned").font(.title3.bold())
                Text(r.new == 0 ? "No new songs." : "\(r.new) new songs, metadata fetched.").foregroundStyle(.secondary)
                if r.unlabelled > 0 { Text("\(r.unlabelled) songs have no vibe labels yet: ask Claude to label them.").font(.caption).foregroundStyle(.secondary) }
            }
            Spacer()
            BrandButton(title: "Done", icon: "house.fill") { model.home() }
        }
    }
}

// MARK: - App ------------------------------------------------------------------------------

/// A borderless panel that can take keyboard input without activating the app, so Music
/// stays the active app while you use it.
final class OrganizerPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// Watches Music: where its window is and whether it's in front. The ✨ button and the panel
/// live inside Music's window, follow it, and hide whenever another app is in front.
@MainActor enum MusicWatcher {
    static let musicID = "com.apple.Music"
    static var timer: Timer?
    static var lastFrame: NSRect?

    static var musicIsFront: Bool { NSWorkspace.shared.frontmostApplication?.bundleIdentifier == musicID }

    static func start() {
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didActivateApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            center.addObserver(forName: name, object: nil, queue: .main) { _ in Task { @MainActor in refresh() } }
        }
        refresh()
    }

    /// Show or hide everything depending on whether Music is in front; follow its window while it is.
    static func refresh() {
        if musicIsFront {
            if timer == nil {
                timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in Task { @MainActor in follow() } }
            }
            follow()
        } else {
            timer?.invalidate()
            timer = nil
            FloatingButton.window?.orderOut(nil)
            FloatingPanel.window?.orderOut(nil)
        }
    }

    static func follow() {
        guard let m = musicFrame() else {
            FloatingButton.window?.orderOut(nil)
            FloatingPanel.window?.orderOut(nil)
            return
        }
        lastFrame = m
        FloatingButton.place(in: m)
        if FloatingPanel.isOpen, let w = FloatingPanel.window {
            FloatingPanel.place(w, in: m, animate: true)
            if !w.isVisible { w.orderFrontRegardless() }
        }
    }

    /// Music's front window in Cocoa screen coordinates, if Music is running and has one.
    static func musicFrame() -> NSRect? {
        guard NSRunningApplication.runningApplications(withBundleIdentifier: musicID).first != nil else { return nil }
        let src = "tell application \"Music\" to if (count of browser windows) > 0 then get bounds of front browser window"
        var err: NSDictionary?
        guard let r = NSAppleScript(source: src)?.executeAndReturnError(&err), r.numberOfItems == 4,
              let primary = NSScreen.screens.first else { return nil }
        let v = (1...4).map { r.atIndex($0)?.doubleValue ?? 0 }  // left, top, right, bottom (top-left origin)
        return NSRect(x: v[0], y: primary.frame.height - v[3], width: v[2] - v[0], height: v[3] - v[1])
    }
}

/// The ✨ button in the top-right corner of Music's window: one tap opens or closes the panel.
@MainActor enum FloatingButton {
    static var window: OrganizerPanel?
    static let side: CGFloat = 36

    static func make() {
        let w = OrganizerPanel(contentRect: NSRect(x: 0, y: 0, width: side, height: side),
                               styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.level = .floating
        w.isReleasedWhenClosed = false
        w.hidesOnDeactivate = false
        w.collectionBehavior = [.fullScreenAuxiliary, .moveToActiveSpace]
        w.contentView = NSHostingView(rootView: SparkleButton().environmentObject(FloatingPanel.model))
        window = w
    }

    static func place(in m: NSRect) {
        guard let w = window else { return }
        // Top-right of Music's window, in the toolbar strip; left of the panel's edge.
        let origin = NSPoint(x: m.maxX - side - 14, y: m.maxY - side - 12)
        if w.frame.origin != origin { w.setFrameOrigin(origin) }
        if !w.isVisible { w.orderFrontRegardless() }
    }
}

struct SparkleButton: View {
    @EnvironmentObject var model: AppModel
    @State private var hover = false
    @State private var tapped = false
    var body: some View {
        Button {
            tapped.toggle()
            FloatingPanel.toggle()
        } label: {
            ZStack {
                Circle().fill(brand)
                Image(systemName: model.panelOpen ? "xmark" : "sparkles")
                    .font(.system(size: 15, weight: .bold)).foregroundStyle(.white)
                    .contentTransition(.symbolEffect(.replace))
                    .symbolEffect(.bounce, value: tapped)
            }
            .frame(width: 32, height: 32)
            .scaleEffect(hover ? 1.12 : 1)
            .shadow(color: .pink.opacity(hover ? 0.6 : 0.3), radius: hover ? 8 : 4)
            .animation(.spring(response: 0.3, dampingFraction: 0.6), value: hover)
        }
        .buttonStyle(.plain)
        .frame(width: FloatingButton.side, height: FloatingButton.side)
        .onHover { hover = $0 }
        .help(model.panelOpen ? "Close Organizer" : "Open Organizer")
    }
}

/// The Organizer panel, docked inside Music's window like a sidebar, under the ✨ button.
@MainActor enum FloatingPanel {
    static var window: OrganizerPanel?
    static let model = AppModel()
    static var keyMonitor: Any?
    static var isOpen = false
    static let size = NSSize(width: 400, height: 600)

    /// Quit completely (the ✨ button goes too, until the next login or Scripts menu → ✨ Organizer).
    static func quit() { NSApp.terminate(nil) }

    static func toggle() { isOpen ? hide() : show() }

    static func hide() {
        isOpen = false
        model.panelOpen = false
        guard let w = window else { return }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.15
            w.animator().alphaValue = 0
        }, completionHandler: { Task { @MainActor in if !isOpen { w.orderOut(nil) } } })
    }

    static func show() {
        // The panel lives in Music: bring Music forward if something else is in front.
        if let music = NSRunningApplication.runningApplications(withBundleIdentifier: MusicWatcher.musicID).first,
           !music.isActive {
            music.activate()
        }
        let w = window ?? makeWindow()
        isOpen = true
        model.panelOpen = true
        model.home()
        if let m = MusicWatcher.musicFrame() { place(w, in: m, animate: false) } else { placeOnScreen(w) }
        w.alphaValue = 0
        w.orderFrontRegardless()
        w.makeKey()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.22
            w.animator().alphaValue = 1
        }
        model.popToken += 1
    }

    static func makeWindow() -> OrganizerPanel {
        let w = OrganizerPanel(contentRect: NSRect(origin: .zero, size: size),
                               styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView],
                               backing: .buffered, defer: false)
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.level = .floating
        w.isReleasedWhenClosed = false
        w.hidesOnDeactivate = false
        w.isMovableByWindowBackground = false
        w.collectionBehavior = [.fullScreenAuxiliary, .moveToActiveSpace]
        let effect = NSVisualEffectView()
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 18
        effect.layer?.masksToBounds = true
        effect.layer?.borderWidth = 0.5
        effect.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
        let host = NSHostingView(rootView: PoppingRoot().environmentObject(model))
        host.frame = effect.bounds
        host.autoresizingMask = [.width, .height]
        effect.addSubview(host)
        w.contentView = effect
        window = w

        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { e in
            let cmd = e.modifierFlags.contains(.command)
            if cmd && e.charactersIgnoringModifiers == "q" { quit(); return nil }        // ⌘Q quits
            if e.keyCode == 53 || (cmd && e.charactersIgnoringModifiers == "w") {      // Esc, ⌘W hide
                hide()
                return nil
            }
            return e
        }
        return w
    }

    /// Dock inside the right side of Music's window, under the ✨ button; beside it if it's narrow.
    static func place(_ w: NSWindow, in m: NSRect, animate: Bool) {
        var frame = NSRect(origin: .zero, size: size)
        frame.size.height = min(size.height, max(420, m.height - 76))
        if m.width >= size.width + 420 {
            frame.origin = NSPoint(x: m.maxX - size.width - 14, y: m.maxY - 56 - frame.height)
        } else {
            frame.origin = NSPoint(x: m.maxX + 8, y: m.maxY - frame.height)
        }
        if abs(w.frame.origin.x - frame.origin.x) > 0.5 || abs(w.frame.origin.y - frame.origin.y) > 0.5
            || abs(w.frame.height - frame.height) > 0.5 {
            w.setFrame(frame, display: true, animate: animate)
        }
    }

    static func placeOnScreen(_ w: NSWindow) {
        guard let screen = NSScreen.main?.visibleFrame else { return }
        w.setFrame(NSRect(x: screen.maxX - size.width - 20, y: screen.maxY - 20 - size.height,
                          width: size.width, height: size.height), display: true)
    }
}

/// Root view that springs in each time the floating panel opens.
struct PoppingRoot: View {
    @EnvironmentObject var model: AppModel
    @State private var shown = false
    var body: some View {
        RootView()
            .scaleEffect(shown ? 1 : 0.9, anchor: .top)
            .opacity(shown ? 1 : 0)
            .onChange(of: model.popToken) { _, _ in
                shown = false
                withAnimation(.spring(response: 0.42, dampingFraction: 0.72)) { shown = true }
            }
            .onAppear { withAnimation(.spring(response: 0.42, dampingFraction: 0.72)) { shown = true } }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ n: Notification) {
        Task { @MainActor in
            FloatingButton.make()
            MusicWatcher.start()
            if CommandLine.arguments.contains("--preview") { FloatingPanel.show() }
        }
    }
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme == "musicorganizer" {
            Task { @MainActor in
                switch url.host {
                case "quit": FloatingPanel.quit()
                case "hide": FloatingPanel.hide()
                case "describe":
                    FloatingPanel.show()
                    let q = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "q" }?.value
                    FloatingPanel.model.pendingDescribe = q
                    FloatingPanel.model.go(.describe)
                default: FloatingPanel.show()
                }
            }
        }
    }
}

/// No menu bar icon and no Dock icon: the app waits in the background and pops its panel over
/// Music when you choose ✨ Organizer in Music's Scripts menu (which opens musicorganizer://show).
@main
struct MusicOrganizerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        Settings { EmptyView() }
    }
}
