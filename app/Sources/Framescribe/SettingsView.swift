import AppKit
import FramescribeCore
import SwiftUI

struct SettingsView: View {
    @Bindable var model: AppModel

    var body: some View {
        Form {
            Section {
                LabeledContent("Output folder") {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(model.outputFolder.path)
                            .font(.system(.callout, design: .monospaced))
                            .textSelection(.enabled)
                            .lineLimit(2)
                            .truncationMode(.middle)
                            .accessibilityLabel("Output folder")
                            .accessibilityValue(model.outputFolder.path)
                        HStack {
                            Button("Choose…") { chooseFolder() }
                                .accessibilityLabel("Choose the output folder")
                            Button("Use Default") { model.settings.outputFolder = nil }
                                .disabled(model.settings.outputFolder == nil)
                                .accessibilityLabel("Use the default output folder")
                        }
                    }
                }
                Text("Each video's results go to a folder named after it inside this one (\"name 2\", \"name 3\", … for another video of the same name). Processing the same video again reuses the stages already computed.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Folder"
        panel.message = "Choose where Framescribe puts its results"
        if panel.runModal() == .OK, let url = panel.url { model.settings.outputFolder = url.path }
    }
}
