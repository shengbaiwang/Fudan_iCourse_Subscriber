import AppKit
import WebKit

final class SeaApplication: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private var backend: Process?
    private var output = ""
    private var baseURL: URL?
    private var stopping = false
    private var quitting = false
    private var startupTimer: Timer?
    private var preferences: [String: String] = [:]
    private var preferencesWrite: DispatchWorkItem?
    private let token = UUID().uuidString
    private let development = Bundle.main.object(forInfoDictionaryKey: "SeaDevelopment") as? [String: String]
    private var applicationName: String { development == nil ? "丰饶之海" : "丰饶之海 Dev" }
    private var configDirectory: URL {
        if let override = ProcessInfo.processInfo.environment["ICOURSE_WEB_CONFIG_DIR"] { return URL(fileURLWithPath: override) }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("fudan-icourse-subscriber")
    }
    private var stateFile: URL { configDirectory.appendingPathComponent("desktop-state.json") }

    private func pageStateScript() -> WKUserScript {
        let data = (try? JSONSerialization.data(withJSONObject: preferences)) ?? Data("{}".utf8)
        let state = String(data: data, encoding: .utf8) ?? "{}"
        let script = """
        window.SEA_DESKTOP = true;
        (() => {
          const state = \(state);
          for (const [key,value] of Object.entries(state)) localStorage.setItem(key,value);
          const send = () => window.webkit.messageHandlers.sea.postMessage({kind:'preferences',values:Object.fromEntries(Object.keys(localStorage).map(key=>[key,localStorage.getItem(key)]))});
          const set = Storage.prototype.setItem, remove = Storage.prototype.removeItem;
          Storage.prototype.setItem = function(key,value) {set.call(this,key,value); if(this===localStorage) send();};
          Storage.prototype.removeItem = function(key) {remove.call(this,key); if(this===localStorage) send();};
          const fetchOriginal = window.fetch.bind(window);
          window.fetch = (input,options={}) => {
            const url = new URL(input instanceof Request ? input.url : input,location.href);
            if(url.origin===location.origin && url.pathname.startsWith('/api/')) {
              const headers = new Headers(input instanceof Request ? input.headers : undefined);
              new Headers(options.headers || {}).forEach((value,key)=>headers.set(key,value));
              headers.set('X-Sea-Session','\(token)'); options={...options,headers};
            }
            return fetchOriginal(input,options);
          };
        })();
        """
        return WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let data = try? Data(contentsOf: stateFile), let value = try? JSONDecoder().decode([String: String].self, from: data) { preferences = value }
        preferences["sea-personal-library"] = "1"
        installMenu()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "sea")
        configuration.userContentController.addUserScript(pageStateScript())
        web = WKWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1320,height: 860), styleMask: [.titled,.closable,.miniaturizable,.resizable], backing: .buffered, defer: false)
        window.title = applicationName
        window.minSize = NSSize(width: 820,height: 560)
        window.setFrameAutosaveName(development == nil ? "SeaMainWindow" : "SeaDevMainWindow")
        window.isReleasedWhenClosed = false
        window.contentView = web
        if development != nil {
            if #available(macOS 13.3, *) { web.isInspectable = true }
        }
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.loadHTMLString("<html><body style='margin:0;background:#fafcfd;color:#1e5085;display:grid;place-items:center;height:100vh;font:18px -apple-system'><div>丰饶之海<span style='display:block;margin-top:15px;font-size:12px;color:#6f747c'>正在打开资料库…</span></div></body></html>",baseURL: nil)
        startBackend()
    }

    private func startBackend() {
        guard let resources = Bundle.main.resourceURL else { fail("没有找到应用资源，请重新构建应用。"); return }
        let process = Process()
        if let development = development {
            guard let root = development["ProjectRoot"], let python = development["PythonExecutable"],
                  FileManager.default.isExecutableFile(atPath: python),
                  FileManager.default.fileExists(atPath: root + "/desktop/backend.py") else {
                fail("开发环境已移动或缺少 Python。请从项目重新运行「启动丰饶之海 Dev.command」。"); return
            }
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = ["-u",root + "/desktop/backend.py","--port","0","--no-open"]
            process.currentDirectoryURL = URL(fileURLWithPath: root)
        } else {
            process.executableURL = resources.appendingPathComponent("backend/sea-backend")
            process.arguments = ["--port","0","--no-open"]
            process.currentDirectoryURL = resources
        }
        var env = ProcessInfo.processInfo.environment
        env["SEA_DESKTOP_TOKEN"] = token
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = development == nil ? FileHandle.nullDevice : FileHandle.standardError
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let value = String(data:data,encoding:.utf8) else { return }
            DispatchQueue.main.async { self?.receive(value) }
        }
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping else { return }
                self.fail("资料库服务已退出。你的资料仍保存在本机，请重新打开应用。")
            }
        }
        backend = process
        do { try process.run() }
        catch { fail("资料库服务无法启动。请确认应用完整后重新打开。"); return }
        startupTimer = Timer.scheduledTimer(withTimeInterval: 40,repeats: false) { [weak self] _ in self?.fail("打开资料库超时，请退出后重试。") }
    }

    private func receive(_ text: String) {
        output += text
        while let end = output.firstIndex(of: "\n") {
            let line = String(output[..<end]); output.removeSubrange(...end)
            if line.hasPrefix("SEA_READY "), let data = line.dropFirst(10).data(using:.utf8),
               let value = try? JSONSerialization.jsonObject(with:data) as? [String:String],
               let address = value["url"], let url = URL(string:address) {
                baseURL = url
                startupTimer?.invalidate()
                web.load(URLRequest(url:url))
            }
        }
        if output.count > 10000 { output = String(output.suffix(10000)) }
    }

    private func fail(_ text: String) {
        startupTimer?.invalidate()
        let alert = NSAlert(); alert.messageText = "丰饶之海"; alert.informativeText = text; alert.addButton(withTitle:"退出")
        alert.runModal(); stopping = true; backend?.terminate(); NSApp.terminate(nil)
    }

    private func installMenu() {
        let main = NSMenu()
        let app = NSMenu(); let appItem = NSMenuItem(); appItem.submenu = app; main.addItem(appItem)
        app.addItem(withTitle:"关于" + applicationName,action:#selector(about),keyEquivalent:"").target = self
        app.addItem(NSMenuItem.separator())
        app.addItem(withTitle:"设置…",action:#selector(settings),keyEquivalent:",").target = self
        app.addItem(NSMenuItem.separator())
        app.addItem(withTitle:"隐藏" + applicationName,action:#selector(NSApplication.hide(_:)),keyEquivalent:"h")
        app.addItem(withTitle:"显示全部",action:#selector(NSApplication.unhideAllApplications(_:)),keyEquivalent:"")
        app.addItem(NSMenuItem.separator())
        app.addItem(withTitle:"退出" + applicationName,action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q")
        let file = NSMenu(title:"文件"); let fileItem = NSMenuItem(title:"文件",action:nil,keyEquivalent:""); fileItem.submenu = file; main.addItem(fileItem)
        file.addItem(withTitle:"新建手记",action:#selector(newNote),keyEquivalent:"n").target = self
        file.addItem(withTitle:"导入 Markdown…",action:#selector(importNotes),keyEquivalent:"o").target = self
        file.addItem(withTitle:"导出手记备份…",action:#selector(backup),keyEquivalent:"").target = self
        file.addItem(NSMenuItem.separator())
        file.addItem(withTitle:"关闭窗口",action:#selector(NSWindow.performClose(_:)),keyEquivalent:"w")
        let edit = NSMenu(title:"编辑"); let editItem = NSMenuItem(title:"编辑",action:nil,keyEquivalent:""); editItem.submenu = edit; main.addItem(editItem)
        for (name,selector,key) in [("撤销","undo:","z"),("重做","redo:","Z"),("剪切","cut:","x"),("复制","copy:","c"),("粘贴","paste:","v"),("全选","selectAll:","a")] {
            edit.addItem(withTitle:name,action:Selector(selector),keyEquivalent:key)
        }
        edit.addItem(NSMenuItem.separator())
        edit.addItem(withTitle:"搜索资料库",action:#selector(search),keyEquivalent:"k").target = self
        let view = NSMenu(title:"显示"); let viewItem = NSMenuItem(title:"显示",action:nil,keyEquivalent:""); viewItem.submenu = view; main.addItem(viewItem)
        view.addItem(withTitle:"切换阅读与编辑",action:#selector(reading),keyEquivalent:"e").target = self
        view.addItem(withTitle:"切换外观",action:#selector(theme),keyEquivalent:"").target = self
        if development != nil {
            view.addItem(NSMenuItem.separator())
            view.addItem(withTitle:"刷新开发界面",action:#selector(reloadDevelopment),keyEquivalent:"r").target = self
        }
        let full = view.addItem(withTitle:"进入全屏",action:#selector(NSWindow.toggleFullScreen(_:)),keyEquivalent:"f"); full.keyEquivalentModifierMask = [.command,.control]
        let windows = NSMenu(title:"窗口"); let windowItem = NSMenuItem(title:"窗口",action:nil,keyEquivalent:""); windowItem.submenu = windows; main.addItem(windowItem)
        windows.addItem(withTitle:"最小化",action:#selector(NSWindow.performMiniaturize(_:)),keyEquivalent:"m")
        NSApp.windowsMenu = windows; NSApp.mainMenu = main
    }
    private func js(_ code: String) { web?.evaluateJavaScript(code,completionHandler:nil) }
    @objc private func newNote() { js("showView('notes');document.getElementById('note-new').click()") }
    @objc private func importNotes() { js("showView('notes');document.getElementById('note-import').click()") }
    @objc private func backup() { js("document.getElementById('notebook-backup').click()") }
    @objc private func settings() { js("showView('settings')") }
    @objc private func search() { js("showView('search');document.getElementById('search').focus()") }
    @objc private func reading() { js("if(document.body.dataset.view==='notes')document.getElementById('note-mode').click()") }
    @objc private func theme() { js("document.getElementById('theme-toggle').click()") }
    @objc private func about() { NSApp.orderFrontStandardAboutPanel(options:[.applicationName:applicationName,.applicationVersion:"1.0.0",.credits:NSAttributedString(string:"一座属于自己的知识库。")]) }
    @objc private func reloadDevelopment() {
        guard development != nil, baseURL != nil, !quitting else { return }
        web.callAsyncJavaScript("if(window.SeaNotebook) await window.SeaNotebook.flush(); return true;",arguments:[:],in:nil,in:.page) { [weak self] result in
            guard let self = self, !self.quitting else { return }
            if case .failure = result {
                let alert = NSAlert(); alert.messageText = "刷新前无法保存手记"; alert.informativeText = "请先保存当前内容，再刷新开发界面。"; alert.beginSheetModal(for:self.window); return
            }
            self.persistPreferences()
            self.web.configuration.userContentController.removeAllUserScripts()
            self.web.configuration.userContentController.addUserScript(self.pageStateScript())
            self.web.reloadFromOrigin()
        }
    }

    func userContentController(_ userContentController: WKUserContentController,didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String:Any], body["kind"] as? String == "preferences", let value = body["values"] as? [String:String] else { return }
        preferences = value
        preferencesWrite?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.persistPreferences() }
        preferencesWrite = work; DispatchQueue.main.asyncAfter(deadline:.now() + .milliseconds(200),execute:work)
    }
    private func persistPreferences() {
        try? FileManager.default.createDirectory(at:configDirectory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        if let data = try? JSONEncoder().encode(preferences) { try? data.write(to:stateFile,options:.atomic); try? FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:stateFile.path) }
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if stopping { return .terminateNow }
        if quitting { return .terminateCancel }
        quitting = true
        web.callAsyncJavaScript("if(window.SeaNotebook) await window.SeaNotebook.flush(); return true;",arguments:[:],in:nil,in:.page) { [weak self] result in
            guard let self = self else { return }
            if case .failure = result, self.baseURL != nil {
                let alert = NSAlert(); alert.messageText = "手记还没有保存"; alert.informativeText = "未保存草稿已保留在本机。可以返回继续编辑，或退出后重新打开。"; alert.addButton(withTitle:"返回编辑"); alert.addButton(withTitle:"退出并保留草稿")
                if alert.runModal() == .alertFirstButtonReturn { self.quitting = false; sender.reply(toApplicationShouldTerminate:false); return }
            }
            self.stopping = true; self.persistPreferences(); self.backend?.terminate()
            if let process = self.backend, process.isRunning {
                DispatchQueue.global().async {
                    process.waitUntilExit()
                    DispatchQueue.main.async { sender.reply(toApplicationShouldTerminate:true) }
                }
            } else { sender.reply(toApplicationShouldTerminate:true) }
        }
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool) -> Bool { window.makeKeyAndOrderFront(nil); return true }

    func webView(_ webView: WKWebView,decidePolicyFor navigationAction: WKNavigationAction,decisionHandler: @escaping (WKNavigationActionPolicy)->Void) {
        if navigationAction.shouldPerformDownload { decisionHandler(.download); return }
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" || url.scheme == "blob" || url.host == baseURL?.host && url.port == baseURL?.port { decisionHandler(.allow) }
        else { if ["http","https","mailto","obsidian"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }; decisionHandler(.cancel) }
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for navigationAction:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }; return nil
    }
    func webView(_ webView:WKWebView,navigationAction:WKNavigationAction,didBecome download:WKDownload) { download.delegate = self }
    func webView(_ webView:WKWebView,decidePolicyFor navigationResponse:WKNavigationResponse,decisionHandler:@escaping (WKNavigationResponsePolicy)->Void) { decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download) }
    func webView(_ webView:WKWebView,navigationResponse:WKNavigationResponse,didBecome download:WKDownload) { download.delegate = self }
    func download(_ download:WKDownload,decideDestinationUsing response:URLResponse,suggestedFilename:String,completionHandler:@escaping (URL?)->Void) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = suggestedFilename; panel.canCreateDirectories = true
        panel.beginSheetModal(for:window) { result in completionHandler(result == .OK ? panel.url : nil) }
    }
    func webView(_ webView:WKWebView,runOpenPanelWith parameters:WKOpenPanelParameters,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping ([URL]?)->Void) {
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = parameters.allowsMultipleSelection; panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for:window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }
    func webView(_ webView:WKWebView,runJavaScriptAlertPanelWithMessage message:String,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping ()->Void) {
        let alert = NSAlert(); alert.messageText = "丰饶之海"; alert.informativeText = message; alert.beginSheetModal(for:window) { _ in completionHandler() }
    }
    func webView(_ webView:WKWebView,runJavaScriptConfirmPanelWithMessage message:String,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping (Bool)->Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle:"继续"); alert.addButton(withTitle:"取消"); alert.beginSheetModal(for:window) { result in completionHandler(result == .alertFirstButtonReturn) }
    }
    func webView(_ webView:WKWebView,runJavaScriptTextInputPanelWithPrompt prompt:String,defaultText:String?,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping (String?)->Void) {
        let alert = NSAlert(); alert.messageText = prompt; let field = NSTextField(string:defaultText ?? ""); field.frame = NSRect(x:0,y:0,width:300,height:25); alert.accessoryView = field; alert.addButton(withTitle:"保存"); alert.addButton(withTitle:"取消"); alert.beginSheetModal(for:window) { result in completionHandler(result == .alertFirstButtonReturn ? field.stringValue : nil) }
    }
}
let application = NSApplication.shared
let delegate = SeaApplication()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
