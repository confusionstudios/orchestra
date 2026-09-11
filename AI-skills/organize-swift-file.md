Organize Swift source files for clarity without changing behavior, with limited support for Objective-C files.

# Organize Swift File

Use this skill when asked to tidy, reorder, group, section, or reorganize a Swift source file; move private implementation below its visible API; or assess whether cohesive responsibilities should be extracted into separate files.

Reorganize structure, not behavior. Preserve the file's public contract and keep the diff easy to verify.

## Workflow

1. Read the applicable repository instructions and nearby file conventions.
2. Inspect the whole target file plus enough call sites, tests, and declarations to identify visibility and language constraints.
3. State the intended organization and what must remain unchanged.
4. Make the smallest structural edit that achieves the requested organization.
5. Review the diff specifically for accidental behavioral or API changes.
6. Validate according to the repository's instructions and the risk of the edit.

## Swift Target Shape

Aim for this shape when Swift's rules permit it:

```swift
final class DeviceClient {
    // Stored state must remain in the main declaration.
    private var packetBuffer = Data()

    init(transport: Transport) { ... }

    func connect() { ... }
    func disconnect() { ... }
}

extension DeviceClient: TransportDelegate {
    func transportDidReceive(_ data: Data) { ... }
}

// MARK: - Connection

private extension DeviceClient {
    func openTransport() { ... }
    func closeTransport() { ... }
}

// MARK: - Packet Decoding

private extension DeviceClient {
    func decodePacket(_ data: Data) { ... }
    func emitMessages() { ... }
}
```

Keep the primary declaration and visible API first. Put each protocol conformance declaration in its own non-private extension whenever that compiles without changing API or behavior, then place private extensions below, grouped by responsibility. A requirement such as a stored property, required initializer, or override may remain in the primary declaration even when the conformance declaration and other requirements move to an extension.

If a conformance must remain on the primary declaration, report the exact language, compiler, runtime, or interoperability constraint. Do not leave it inline merely because one requirement cannot move.

Preserve signatures, selectors, annotations, access levels, initialization order, declaration ownership, target membership, and call-site behavior. Do not rename symbols, rewrite logic, or perform adjacent cleanup unless organization requires it.

## Objective-C

Keep public or externally used entry points first. Put private helpers below them, grouped under meaningful `#pragma mark - Connection`, `#pragma mark - Parsing`, or similarly specific sections. Preserve declarations, selectors, and behavior; do not manufacture categories merely to imitate the Swift structure.

## File Extraction Checkpoint

If the file still contains multiple independently cohesive responsibilities after in-file organization, propose exact extractions before creating new files. For each proposed file, name the declarations to move and explain the boundary in one sentence.

Create or modify project files only when the user has authorized splitting. Follow repository-specific tooling and target-membership instructions when doing so.

## Completion

Report:

- what was reorganized;
- whether behavior and API remained unchanged;
- verification performed;
- any file extractions worth considering but not made.
