import Foundation
import KeryxKit

extension AppModel {
    /// A model already holding what `client` answers, for previews and snapshots: no
    /// polling, nothing awaited. With a `.failure` location it is the not-found state.
    public static func preview(
        client: PreviewClient = PreviewClient(),
        location: Result<KeryxLocation, LocateFailure> = .success(.fixture)
    ) -> AppModel {
        let model = AppModel(locator: PreviewLocator(location), makeClient: { _ in client },
                             preferences: Preferences(), timing: Timing(poll: nil))
        model.started = true
        switch location {
        case let .failure(failure):
            model.location = Loadable(error: .notFound(failure))
            return model
        case let .success(found):
            model.location = Loadable(value: found, updatedAt: Date())
        }
        model.client = client
        model.status = .settled(client.statusReply)
        model.doctor = .settled(client.doctorReply)
        model.tasks = .settled(client.tasksReply.map { $0.filter { !$0.internal } })
        model.usage = .settled(client.usageReply)
        model.models = .settled(client.modelsReply)
        model.config = .settled(client.configReply)
        model.approvals = .settled(client.approvalsReply)
        model.memory = .settled(client.memoryReply)
        model.voice = .settled(client.voiceReply)
        model.setupPlan = .settled(client.setupPlanReply)
        model.pin = .settled(client.pinReply)
        model.service = .settled(client.serviceReply)
        model.phoneNumbers = .settled(client.phoneNumbersReply)
        model.phoneWebhooks = .settled(client.phoneWebhooksReply)
        model.phoneTunnel = .settled(client.phoneTunnelReply)
        model.integrations = .settled(client.integrationsReply)
        model.plugins = .settled(client.pluginsReply)
        model.tools = .settled(client.toolsReply)
        model.sshHosts = .settled(client.sshHostsReply)
        // Every list a screen would ask for, so the menus show it from the first frame.
        for row in (try? client.configReply.get()) ?? [] where row.hasOptions {
            model.options[row.key] = .settled(client.optionsReply[row.key] ?? .success(PreviewData.builtinOptions(row.key)))
        }
        if case let .success(detail) = client.taskReply {
            model.taskDetail[detail.task.id] = .settled(.success(detail))
        }
        return model
    }
}

extension Loadable {
    /// Loaded, or failed, now.
    static func settled(_ result: Result<Value, KeryxError>) -> Self {
        switch result {
        case let .success(value): Loadable(value: value, updatedAt: Date())
        case let .failure(error): Loadable(error: error)
        }
    }
}

extension KeryxLocation {
    /// A checkout run through `uv`, for previews and tests. Nothing on disk is consulted.
    public static let fixture = KeryxLocation(
        checkout: "/Users/you/code/keryx",
        executable: "/Users/you/.local/bin/uv",
        prefixArguments: ["run", "--project", "/Users/you/code/keryx", "--no-sync", "keryx"],
        environment: Sanitise.environment([
            "HOME": "/Users/you",
            "PATH": "/Users/you/.local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            "KERYX_HOME": "/Users/you/.config/keryx",
        ]),
        workingDirectory: "/Users/you/code/keryx",
        source: .launchAgent)
}
