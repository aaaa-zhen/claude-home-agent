import AppKit
import Carbon
import Darwin
import EventKit
import Foundation

private let bridgeVersion = "0.1.0"
private let defaultContainer = "Home Agent"
private let defaultAppleAccount = "iCloud"

enum BridgeError: LocalizedError {
    case usage(String)
    case permission(String)
    case notFound(String)
    case invalid(String)
    case automation(String)

    var errorDescription: String? {
        switch self {
        case .usage(let message), .permission(let message), .notFound(let message),
             .invalid(let message), .automation(let message):
            return message
        }
    }
}

struct ParsedArgs {
    private(set) var values: [String]

    init(_ values: ArraySlice<String>) {
        self.values = Array(values)
    }

    mutating func flag(_ name: String) -> Bool {
        guard let index = values.firstIndex(of: name) else { return false }
        values.remove(at: index)
        return true
    }

    mutating func option(_ name: String, default fallback: String? = nil) throws -> String? {
        guard let index = values.firstIndex(of: name) else { return fallback }
        guard values.indices.contains(index + 1) else {
            throw BridgeError.usage("Missing value for \(name)")
        }
        let value = values[index + 1]
        values.removeSubrange(index...index + 1)
        return value
    }

    mutating func required(_ name: String) throws -> String {
        guard let value = try option(name), !value.isEmpty else {
            throw BridgeError.usage("Required option missing: \(name)")
        }
        return value
    }
}

func printJSON(_ object: [String: Any]) {
    do {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
        if let index = CommandLine.arguments.firstIndex(of: "--result-file"),
           CommandLine.arguments.indices.contains(index + 1) {
            try data.write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]), options: .atomic)
        }
    } catch {
        FileHandle.standardOutput.write(Data("{\"ok\":false,\"error\":\"JSON serialization failed\"}\n".utf8))
    }
}

func success(action: String, data: Any, dryRun: Bool = false) -> [String: Any] {
    [
        "ok": true,
        "action": action,
        "dry_run": dryRun,
        "data": data,
        "version": bridgeVersion,
    ]
}

func failure(_ error: Error) -> [String: Any] {
    [
        "ok": false,
        "error": error.localizedDescription,
        "error_type": String(describing: type(of: error)),
        "version": bridgeVersion,
    ]
}

func isoString(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}

func parseDate(_ text: String) throws -> Date {
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = fractional.date(from: text) { return date }
    let normal = ISO8601DateFormatter()
    normal.formatOptions = [.withInternetDateTime]
    if let date = normal.date(from: text) { return date }
    throw BridgeError.invalid(
        "Invalid ISO-8601 date: \(text). Use a timezone, for example 2026-07-12T15:00:00+08:00"
    )
}

func authorizationName(_ status: EKAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorized: return "authorized"
    case .fullAccess: return "full_access"
    case .writeOnly: return "write_only"
    @unknown default: return "unknown"
    }
}

func hasCalendarReadAccess() -> Bool {
    let status = authorizationName(EKEventStore.authorizationStatus(for: .event))
    return status == "full_access" || status == "authorized"
}

func hasCalendarWriteAccess() -> Bool {
    let status = authorizationName(EKEventStore.authorizationStatus(for: .event))
    return status == "full_access" || status == "write_only" || status == "authorized"
}

func hasReminderAccess() -> Bool {
    let status = authorizationName(EKEventStore.authorizationStatus(for: .reminder))
    return status == "full_access" || status == "authorized"
}

func requireCalendarRead() throws {
    guard hasCalendarReadAccess() else {
        throw BridgeError.permission(
            "Calendar full access is not granted. Run: apple-bridge permissions request --calendar"
        )
    }
}

func requireCalendarWrite() throws {
    guard hasCalendarWriteAccess() else {
        throw BridgeError.permission(
            "Calendar write access is not granted. Run: apple-bridge permissions request --calendar"
        )
    }
}

func requireReminders() throws {
    guard hasReminderAccess() else {
        throw BridgeError.permission(
            "Reminders access is not granted. Run: apple-bridge permissions request --reminders"
        )
    }
}

func requestEventPermission(_ store: EKEventStore) -> [String: Any] {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    var message = ""
    store.requestFullAccessToEvents { allowed, error in
        granted = allowed
        message = error?.localizedDescription ?? ""
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 120)
    return ["granted": granted, "error": message]
}

func requestReminderPermission(_ store: EKEventStore) -> [String: Any] {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    var message = ""
    store.requestFullAccessToReminders { allowed, error in
        granted = allowed
        message = error?.localizedDescription ?? ""
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 120)
    return ["granted": granted, "error": message]
}

func appleScriptLiteral(_ value: String) -> String {
    let normalized = value.replacingOccurrences(of: "\r\n", with: "\n")
        .replacingOccurrences(of: "\r", with: "\n")
    let parts = normalized.components(separatedBy: "\n").map { part -> String in
        let escaped = part
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }
    return parts.isEmpty ? "\"\"" : parts.joined(separator: " & linefeed & ")
}

func htmlEscape(_ value: String) -> String {
    value
        .replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
        .replacingOccurrences(of: "\"", with: "&quot;")
        .replacingOccurrences(of: "'", with: "&#39;")
        .replacingOccurrences(of: "\n", with: "<br>")
}

func descriptorValue(_ descriptor: NSAppleEventDescriptor) -> Any {
    if descriptor.descriptorType == typeAEList {
        if descriptor.numberOfItems == 0 { return [Any]() }
        return (1...descriptor.numberOfItems).compactMap { index -> Any? in
            guard let item = descriptor.atIndex(index) else { return nil }
            return descriptorValue(item)
        }
    }
    if descriptor.descriptorType == typeBoolean {
        return descriptor.booleanValue
    }
    if [typeSInt16, typeSInt32, typeUInt32].contains(descriptor.descriptorType) {
        return Int(descriptor.int32Value)
    }
    return descriptor.stringValue ?? ""
}

func runAppleScript(_ source: String) throws -> Any {
    var details: NSDictionary?
    guard let script = NSAppleScript(source: source) else {
        throw BridgeError.automation("Unable to compile AppleScript")
    }
    let result = script.executeAndReturnError(&details)
    if let details {
        let message = (details[NSAppleScript.errorMessage] as? String) ?? "Unknown AppleScript error"
        let number = details[NSAppleScript.errorNumber] ?? "unknown"
        throw BridgeError.automation("Apple automation failed (\(number)): \(message)")
    }
    return descriptorValue(result)
}

func source(named name: String, in store: EKEventStore, entity: EKEntityType) throws -> EKSource {
    let entitySourceIDs = Set(store.calendars(for: entity).map(\.source.sourceIdentifier))
    if let exact = store.sources.first(where: {
        $0.title.caseInsensitiveCompare(name) == .orderedSame && entitySourceIDs.contains($0.sourceIdentifier)
    }) {
        return exact
    }
    if name.caseInsensitiveCompare("iCloud") == .orderedSame,
       let cloud = store.sources.first(where: {
           $0.sourceType == .calDAV && $0.title.lowercased().contains("icloud")
             && entitySourceIDs.contains($0.sourceIdentifier)
       }) {
        return cloud
    }
    let fallback: EKSource?
    if entity == .event {
        fallback = store.defaultCalendarForNewEvents?.source
    } else {
        fallback = store.defaultCalendarForNewReminders()?.source
    }
    guard let fallback else {
        throw BridgeError.notFound("Apple source not found: \(name)")
    }
    if name.caseInsensitiveCompare("default") == .orderedSame { return fallback }
    throw BridgeError.notFound(
        "Apple source '\(name)' was not found. Available sources: \(store.sources.map(\.title).joined(separator: ", "))"
    )
}

func findCalendar(
    title: String,
    sourceName: String?,
    entity: EKEntityType,
    store: EKEventStore
) throws -> EKCalendar {
    let matches = store.calendars(for: entity).filter { calendar in
        calendar.title == title && (sourceName == nil || calendar.source.title.caseInsensitiveCompare(sourceName!) == .orderedSame)
    }
    guard let calendar = matches.first else {
        throw BridgeError.notFound("\(entity == .event ? "Calendar" : "Reminder list") not found: \(title)")
    }
    return calendar
}

func ensureCalendar(
    title: String,
    sourceName: String,
    entity: EKEntityType,
    store: EKEventStore
) throws -> (EKCalendar, Bool) {
    if let existing = store.calendars(for: entity).first(where: {
        $0.title == title && $0.source.title.caseInsensitiveCompare(sourceName) == .orderedSame
    }) {
        return (existing, false)
    }
    let targetSource = try source(named: sourceName, in: store, entity: entity)
    let calendar = EKCalendar(for: entity, eventStore: store)
    calendar.title = title
    calendar.source = targetSource
    try store.saveCalendar(calendar, commit: true)
    return (calendar, true)
}

func eventObject(_ event: EKEvent) -> [String: Any] {
    [
        "id": event.eventIdentifier ?? "",
        "title": event.title ?? "",
        "start": isoString(event.startDate),
        "end": isoString(event.endDate),
        "all_day": event.isAllDay,
        "location": event.location ?? "",
        "notes": event.notes ?? "",
        "calendar": event.calendar.title,
        "source": event.calendar.source.title,
        "url": event.url?.absoluteString ?? "",
    ]
}

func reminderObject(_ reminder: EKReminder) -> [String: Any] {
    var due = ""
    if let components = reminder.dueDateComponents,
       let date = Calendar.current.date(from: components) {
        due = isoString(date)
    }
    return [
        "id": reminder.calendarItemIdentifier,
        "title": reminder.title ?? "",
        "notes": reminder.notes ?? "",
        "due": due,
        "priority": reminder.priority,
        "completed": reminder.isCompleted,
        "completion_date": reminder.completionDate.map(isoString) ?? "",
        "list": reminder.calendar.title,
        "source": reminder.calendar.source.title,
    ]
}

func fetchReminders(store: EKEventStore, calendars: [EKCalendar]?) -> [EKReminder] {
    let predicate = store.predicateForReminders(in: calendars)
    let semaphore = DispatchSemaphore(value: 0)
    var output: [EKReminder] = []
    store.fetchReminders(matching: predicate) { reminders in
        output = reminders ?? []
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 30)
    return output
}

func handleDoctor() -> [String: Any] {
    let notesDictionary = "/System/Applications/Notes.app/Contents/Resources/Notes.sdef"
    return success(action: "doctor", data: [
        "platform": ProcessInfo.processInfo.operatingSystemVersionString,
        "calendar_permission": authorizationName(EKEventStore.authorizationStatus(for: .event)),
        "reminders_permission": authorizationName(EKEventStore.authorizationStatus(for: .reminder)),
        "notes_automation": "not_checked",
        "notes_dictionary_available": FileManager.default.fileExists(atPath: notesDictionary),
        "default_container": defaultContainer,
        "default_account": defaultAppleAccount,
        "delete_operations_exposed": false,
    ])
}

func handlePermissions(_ raw: ArraySlice<String>) throws -> [String: Any] {
    guard raw.first == "request" else {
        return success(action: "permissions.status", data: [
            "calendar": authorizationName(EKEventStore.authorizationStatus(for: .event)),
            "reminders": authorizationName(EKEventStore.authorizationStatus(for: .reminder)),
            "notes": "not_checked",
        ])
    }
    var args = ParsedArgs(raw.dropFirst())
    let requestedCalendar = args.flag("--calendar")
    let requestedReminders = args.flag("--reminders")
    let requestedNotes = args.flag("--notes")
    let requestAll = !requestedCalendar && !requestedReminders && !requestedNotes
    let store = EKEventStore()
    var data: [String: Any] = [:]
    if requestAll || requestedCalendar { data["calendar"] = requestEventPermission(store) }
    if requestAll || requestedReminders { data["reminders"] = requestReminderPermission(store) }
    if requestAll || requestedNotes {
        do {
            _ = try runAppleScript("tell application \"Notes\" to return name")
            data["notes"] = ["granted": true]
        } catch {
            data["notes"] = ["granted": false, "error": error.localizedDescription]
        }
    }
    data["calendar_status"] = authorizationName(EKEventStore.authorizationStatus(for: .event))
    data["reminders_status"] = authorizationName(EKEventStore.authorizationStatus(for: .reminder))
    return success(action: "permissions.request", data: data)
}

func handleCalendar(_ raw: ArraySlice<String>) throws -> [String: Any] {
    guard let command = raw.first else { throw BridgeError.usage("calendar requires list, events, or create") }
    var args = ParsedArgs(raw.dropFirst())
    let store = EKEventStore()
    switch command {
    case "list":
        try requireCalendarRead()
        let calendars = store.calendars(for: .event).map { calendar in
            [
                "id": calendar.calendarIdentifier,
                "name": calendar.title,
                "source": calendar.source.title,
                "writable": calendar.allowsContentModifications,
            ] as [String: Any]
        }
        return success(action: "calendar.list", data: calendars)
    case "events":
        try requireCalendarRead()
        let from = try parseDate(try args.required("--from"))
        let to = try parseDate(try args.required("--to"))
        guard to > from else { throw BridgeError.invalid("--to must be after --from") }
        let calendarName = try args.option("--calendar")
        let calendars = try calendarName.map { [try findCalendar(title: $0, sourceName: nil, entity: .event, store: store)] }
        let predicate = store.predicateForEvents(withStart: from, end: to, calendars: calendars)
        let events = store.events(matching: predicate).sorted { $0.startDate < $1.startDate }.map(eventObject)
        return success(action: "calendar.events", data: events)
    case "create":
        let title = try args.required("--title")
        let start = try parseDate(try args.required("--start"))
        let end = try parseDate(try args.required("--end"))
        guard end > start else { throw BridgeError.invalid("--end must be after --start") }
        let calendarName = try args.option("--calendar", default: defaultContainer)!
        let location = try args.option("--location", default: "")!
        let notes = try args.option("--notes", default: "")!
        let allDay = args.flag("--all-day")
        let apply = args.flag("--apply")
        let proposal: [String: Any] = [
            "title": title, "start": isoString(start), "end": isoString(end),
            "calendar": calendarName, "location": location, "notes": notes, "all_day": allDay,
        ]
        if !apply { return success(action: "calendar.create", data: proposal, dryRun: true) }
        try requireCalendarWrite()
        let calendar = try findCalendar(title: calendarName, sourceName: nil, entity: .event, store: store)
        guard calendar.allowsContentModifications else { throw BridgeError.permission("Calendar is read-only: \(calendarName)") }
        let event = EKEvent(eventStore: store)
        event.calendar = calendar
        event.title = title
        event.startDate = start
        event.endDate = end
        event.isAllDay = allDay
        event.location = location.isEmpty ? nil : location
        event.notes = notes.isEmpty ? nil : notes
        try store.save(event, span: .thisEvent, commit: true)
        return success(action: "calendar.create", data: eventObject(event))
    default:
        throw BridgeError.usage("Unknown calendar command: \(command)")
    }
}

func handleReminders(_ raw: ArraySlice<String>) throws -> [String: Any] {
    guard let command = raw.first else { throw BridgeError.usage("reminders requires lists, list, create, or complete") }
    var args = ParsedArgs(raw.dropFirst())
    let store = EKEventStore()
    switch command {
    case "lists":
        try requireReminders()
        let lists = store.calendars(for: .reminder).map { calendar in
            [
                "id": calendar.calendarIdentifier,
                "name": calendar.title,
                "source": calendar.source.title,
                "writable": calendar.allowsContentModifications,
            ] as [String: Any]
        }
        return success(action: "reminders.lists", data: lists)
    case "list":
        try requireReminders()
        let listName = try args.option("--list")
        let includeCompleted = args.flag("--include-completed")
        let calendars = try listName.map { [try findCalendar(title: $0, sourceName: nil, entity: .reminder, store: store)] }
        let reminders = fetchReminders(store: store, calendars: calendars)
            .filter { includeCompleted || !$0.isCompleted }
            .sorted { lhs, rhs in
                let left = lhs.dueDateComponents.flatMap { Calendar.current.date(from: $0) } ?? .distantFuture
                let right = rhs.dueDateComponents.flatMap { Calendar.current.date(from: $0) } ?? .distantFuture
                return left < right
            }
            .map(reminderObject)
        return success(action: "reminders.list", data: reminders)
    case "create":
        let title = try args.required("--title")
        let listName = try args.option("--list", default: defaultContainer)!
        let notes = try args.option("--notes", default: "")!
        let dueText = try args.option("--due")
        let due = try dueText.map(parseDate)
        let priorityText = try args.option("--priority", default: "0")!
        guard let priority = Int(priorityText), (0...9).contains(priority) else {
            throw BridgeError.invalid("Priority must be between 0 and 9")
        }
        let apply = args.flag("--apply")
        let proposal: [String: Any] = [
            "title": title, "list": listName, "notes": notes,
            "due": due.map(isoString) ?? "", "priority": priority,
        ]
        if !apply { return success(action: "reminders.create", data: proposal, dryRun: true) }
        try requireReminders()
        let calendar = try findCalendar(title: listName, sourceName: nil, entity: .reminder, store: store)
        guard calendar.allowsContentModifications else { throw BridgeError.permission("Reminder list is read-only: \(listName)") }
        let reminder = EKReminder(eventStore: store)
        reminder.calendar = calendar
        reminder.title = title
        reminder.notes = notes.isEmpty ? nil : notes
        reminder.priority = priority
        if let due {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.calendar, .timeZone, .year, .month, .day, .hour, .minute, .second], from: due
            )
            reminder.alarms = [EKAlarm(absoluteDate: due)]
        }
        try store.save(reminder, commit: true)
        return success(action: "reminders.create", data: reminderObject(reminder))
    case "complete":
        let id = try args.required("--id")
        let apply = args.flag("--apply")
        if !apply {
            return success(action: "reminders.complete", data: ["id": id, "completed": true], dryRun: true)
        }
        try requireReminders()
        guard let reminder = store.calendarItem(withIdentifier: id) as? EKReminder else {
            throw BridgeError.notFound("Reminder not found: \(id)")
        }
        reminder.isCompleted = true
        reminder.completionDate = Date()
        try store.save(reminder, commit: true)
        return success(action: "reminders.complete", data: reminderObject(reminder))
    default:
        throw BridgeError.usage("Unknown reminders command: \(command)")
    }
}

func noteRows(_ value: Any, fields: [String]) -> [[String: Any]] {
    guard let rows = value as? [Any] else { return [] }
    return rows.compactMap { row in
        guard let values = row as? [Any] else { return nil }
        var output: [String: Any] = [:]
        for (index, field) in fields.enumerated() where index < values.count {
            let text = String(describing: values[index])
            output[field] = field == "plaintext" ? String(text.prefix(800)) : text
        }
        return output
    }
}

func handleNotes(_ raw: ArraySlice<String>) throws -> [String: Any] {
    guard let command = raw.first else { throw BridgeError.usage("notes requires folders, search, create, or append") }
    var args = ParsedArgs(raw.dropFirst())
    let account = try args.option("--account", default: defaultAppleAccount)!
    switch command {
    case "folders":
        let script = """
        tell application "Notes"
          set output to {}
          repeat with currentAccount in accounts
            repeat with currentFolder in folders of currentAccount
              set end of output to {name of currentAccount as text, name of currentFolder as text}
            end repeat
          end repeat
          return output
        end tell
        """
        return success(action: "notes.folders", data: noteRows(try runAppleScript(script), fields: ["account", "folder"]))
    case "search":
        let query = try args.required("--query")
        let folder = try args.option("--folder", default: defaultContainer)!
        let limitText = try args.option("--limit", default: "10")!
        guard let limit = Int(limitText), limit > 0, limit <= 50 else {
            throw BridgeError.invalid("--limit must be between 1 and 50")
        }
        let script = """
        tell application "Notes"
          set targetAccount to first account whose name is \(appleScriptLiteral(account))
          set targetFolder to first folder of targetAccount whose name is \(appleScriptLiteral(folder))
          set searchText to \(appleScriptLiteral(query))
          set output to {}
          repeat with currentNote in notes of targetFolder
            try
              set noteName to name of currentNote as text
              set noteText to plaintext of currentNote as text
              if noteName contains searchText or noteText contains searchText then
                set end of output to {id of currentNote as text, noteName, noteText}
                if (count of output) is greater than or equal to \(limit) then exit repeat
              end if
            end try
          end repeat
          return output
        end tell
        """
        return success(action: "notes.search", data: noteRows(try runAppleScript(script), fields: ["id", "title", "plaintext"]))
    case "create":
        let title = try args.required("--title")
        let body = try args.option("--body", default: "")!
        let folder = try args.option("--folder", default: defaultContainer)!
        let apply = args.flag("--apply")
        let proposal = ["title": title, "body": body, "folder": folder, "account": account]
        if !apply { return success(action: "notes.create", data: proposal, dryRun: true) }
        let html = "<div><b>\(htmlEscape(title))</b></div><div>\(htmlEscape(body))</div>"
        let script = """
        tell application "Notes"
          set targetAccount to first account whose name is \(appleScriptLiteral(account))
          set targetFolder to first folder of targetAccount whose name is \(appleScriptLiteral(folder))
          set createdNote to make new note at targetFolder with properties {name:\(appleScriptLiteral(title)), body:\(appleScriptLiteral(html))}
          return {id of createdNote as text, name of createdNote as text}
        end tell
        """
        let rows = noteRows([try runAppleScript(script)], fields: ["id", "title"])
        return success(action: "notes.create", data: rows.first ?? proposal)
    case "append":
        let id = try args.required("--id")
        let text = try args.required("--text")
        let apply = args.flag("--apply")
        if !apply { return success(action: "notes.append", data: ["id": id, "text": text], dryRun: true) }
        let html = "<div>\(htmlEscape(text))</div>"
        let script = """
        tell application "Notes"
          set targetAccount to first account whose name is \(appleScriptLiteral(account))
          set targetNote to first note of targetAccount whose id is \(appleScriptLiteral(id))
          set body of targetNote to (body of targetNote) & \(appleScriptLiteral(html))
          return {id of targetNote as text, name of targetNote as text}
        end tell
        """
        let rows = noteRows([try runAppleScript(script)], fields: ["id", "title"])
        return success(action: "notes.append", data: rows.first ?? ["id": id])
    default:
        throw BridgeError.usage("Unknown notes command: \(command)")
    }
}

func handleInitialize(_ raw: ArraySlice<String>) throws -> [String: Any] {
    var args = ParsedArgs(raw)
    let apply = args.flag("--apply")
    let container = try args.option("--name", default: defaultContainer)!
    let sourceName = try args.option("--source", default: defaultAppleAccount)!
    let accountName = try args.option("--account", default: defaultAppleAccount)!
    let proposal: [String: Any] = [
        "calendar": container,
        "reminder_list": container,
        "notes_folder": container,
        "source": sourceName,
        "account": accountName,
    ]
    if !apply { return success(action: "initialize", data: proposal, dryRun: true) }
    try requireCalendarRead()
    try requireReminders()
    let store = EKEventStore()
    let (calendar, calendarCreated) = try ensureCalendar(title: container, sourceName: sourceName, entity: .event, store: store)
    let (list, listCreated) = try ensureCalendar(title: container, sourceName: sourceName, entity: .reminder, store: store)
    let script = """
    tell application "Notes"
      set targetAccount to first account whose name is \(appleScriptLiteral(accountName))
      set wasCreated to false
      if not (exists folder \(appleScriptLiteral(container)) of targetAccount) then
        make new folder at targetAccount with properties {name:\(appleScriptLiteral(container))}
        set wasCreated to true
      end if
      return {name of targetAccount as text, \(appleScriptLiteral(container)), wasCreated}
    end tell
    """
    let noteResult = try runAppleScript(script)
    return success(action: "initialize", data: [
        "calendar": ["name": calendar.title, "source": calendar.source.title, "created": calendarCreated],
        "reminder_list": ["name": list.title, "source": list.source.title, "created": listCreated],
        "notes": noteResult,
    ])
}

func usage() -> String {
    """
    apple-bridge doctor
    apple-bridge permissions [request [--calendar] [--reminders] [--notes]]
    apple-bridge initialize [--name "Home Agent"] [--source iCloud] [--apply]
    apple-bridge calendar list
    apple-bridge calendar events --from ISO --to ISO [--calendar NAME]
    apple-bridge calendar create --title TEXT --start ISO --end ISO [--calendar NAME] [--apply]
    apple-bridge reminders lists
    apple-bridge reminders list [--list NAME] [--include-completed]
    apple-bridge reminders create --title TEXT [--due ISO] [--list NAME] [--apply]
    apple-bridge reminders complete --id ID [--apply]
    apple-bridge notes folders
    apple-bridge notes search --query TEXT [--folder NAME]
    apple-bridge notes create --title TEXT [--body TEXT] [--folder NAME] [--apply]
    apple-bridge notes append --id ID --text TEXT [--apply]

    Write operations are previews unless --apply is present. Delete is not exposed.
    """
}

func run() throws -> [String: Any] {
    let arguments = CommandLine.arguments.dropFirst()
    guard let command = arguments.first else { throw BridgeError.usage(usage()) }
    let rest = arguments.dropFirst()
    switch command {
    case "doctor": return handleDoctor()
    case "permissions": return try handlePermissions(rest)
    case "initialize", "init": return try handleInitialize(rest)
    case "calendar": return try handleCalendar(rest)
    case "reminders": return try handleReminders(rest)
    case "notes": return try handleNotes(rest)
    case "help", "--help", "-h": return success(action: "help", data: ["usage": usage()])
    default: throw BridgeError.usage("Unknown command: \(command)\n\n\(usage())")
    }
}

do {
    printJSON(try run())
} catch {
    printJSON(failure(error))
    exit(1)
}
