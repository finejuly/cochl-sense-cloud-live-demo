#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <signal.h>

@interface AppDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSTextField *statusLabel;
@property(nonatomic, strong) NSTask *serverTask;
@property(nonatomic, strong) NSMutableString *outputBuffer;
@property(nonatomic, strong) NSURL *projectRoot;
@property(nonatomic, copy) NSString *serverScheme;
@property(nonatomic, copy) NSString *serverHost;
@property(nonatomic, assign) NSInteger serverPort;
@property(nonatomic, assign) BOOL isQuitting;
@property(nonatomic, assign) BOOL compactWindow;
@property(nonatomic, assign) NSRect expandedWindowFrame;
@property(nonatomic, assign) NSSize expandedWindowMinSize;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
  [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
  self.outputBuffer = [NSMutableString string];
  self.projectRoot = [self resolveProjectRoot];
  [self createWindow];
  [self startServer];
  [NSApp activateIgnoringOtherApps:YES];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
  return YES;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
  self.isQuitting = YES;
  [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"windowMode"];
  [self stopServer];
}

- (void)windowWillClose:(NSNotification *)notification {
  self.isQuitting = YES;
  [self stopServer];
  [NSApp terminate:nil];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
  self.statusLabel.hidden = YES;
  self.webView.hidden = NO;
}

- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error {
  [self showStatus:[NSString stringWithFormat:@"Cochl.Sense Cloud Live Demo를 열 수 없습니다: %@", error.localizedDescription]];
}

- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
  [self showStatus:[NSString stringWithFormat:@"Cochl.Sense Cloud Live Demo를 열 수 없습니다: %@", error.localizedDescription]];
}

- (void)webView:(WKWebView *)webView
    requestMediaCapturePermissionForOrigin:(WKSecurityOrigin *)origin
    initiatedByFrame:(WKFrameInfo *)frame
    type:(WKMediaCaptureType)type
    decisionHandler:(void (^)(WKPermissionDecision decision))decisionHandler {
  BOOL isAllowedOrigin = self.serverScheme.length > 0 &&
    origin.protocol.length > 0 &&
    origin.host.length > 0 &&
    [origin.protocol caseInsensitiveCompare:self.serverScheme] == NSOrderedSame &&
    [origin.host caseInsensitiveCompare:self.serverHost] == NSOrderedSame &&
    origin.port == self.serverPort;
  if (type == WKMediaCaptureTypeMicrophone && isAllowedOrigin) {
    decisionHandler(WKPermissionDecisionGrant);
    return;
  }
  decisionHandler(WKPermissionDecisionDeny);
}

- (void)webView:(WKWebView *)webView
    decidePolicyForNavigationAction:(WKNavigationAction *)navigationAction
    decisionHandler:(void (^)(WKNavigationActionPolicy policy))decisionHandler {
  NSURL *url = navigationAction.request.URL;
  NSString *scheme = url.scheme.lowercaseString;
  BOOL isInitialPage = [scheme isEqualToString:@"about"];
  BOOL isBlobUrl = [scheme isEqualToString:@"blob"];
  BOOL isServerOrigin = self.serverScheme.length > 0 &&
    url.host.length > 0 &&
    [scheme isEqualToString:self.serverScheme.lowercaseString] &&
    [url.host caseInsensitiveCompare:self.serverHost] == NSOrderedSame &&
    (url.port ? url.port.integerValue : 80) == self.serverPort;
  decisionHandler(
    (isInitialPage || isBlobUrl || isServerOrigin)
      ? WKNavigationActionPolicyAllow
      : WKNavigationActionPolicyCancel
  );
}

- (NSURL *)resolveProjectRoot {
  NSURL *appParent = [[[NSBundle mainBundle] bundleURL] URLByDeletingLastPathComponent];
  NSString *backendPath = [[appParent URLByAppendingPathComponent:@"backend/app/main.py"] path];
  if ([[NSFileManager defaultManager] fileExistsAtPath:backendPath]) {
    return appParent;
  }
  return [NSURL fileURLWithPath:[[NSFileManager defaultManager] currentDirectoryPath]];
}

- (void)createWindow {
  WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
  configuration.preferences.javaScriptCanOpenWindowsAutomatically = YES;
  configuration.mediaTypesRequiringUserActionForPlayback = WKAudiovisualMediaTypeNone;
  configuration.userContentController = [[WKUserContentController alloc] init];
  [configuration.userContentController addScriptMessageHandler:self name:@"windowMode"];

  self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
  self.webView.navigationDelegate = self;
  self.webView.UIDelegate = self;
  self.webView.translatesAutoresizingMaskIntoConstraints = NO;
  self.webView.hidden = YES;

  self.statusLabel = [NSTextField labelWithString:@"Cochl.Sense Cloud Live Demo를 시작하는 중..."];
  self.statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
  self.statusLabel.alignment = NSTextAlignmentCenter;
  self.statusLabel.font = [NSFont systemFontOfSize:15 weight:NSFontWeightMedium];
  self.statusLabel.textColor = [NSColor secondaryLabelColor];
  self.statusLabel.maximumNumberOfLines = 0;

  NSView *contentView = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 1100, 760)];
  contentView.wantsLayer = YES;
  contentView.layer.backgroundColor = NSColor.windowBackgroundColor.CGColor;
  [contentView addSubview:self.webView];
  [contentView addSubview:self.statusLabel];

  [NSLayoutConstraint activateConstraints:@[
    [self.webView.leadingAnchor constraintEqualToAnchor:contentView.leadingAnchor],
    [self.webView.trailingAnchor constraintEqualToAnchor:contentView.trailingAnchor],
    [self.webView.topAnchor constraintEqualToAnchor:contentView.topAnchor],
    [self.webView.bottomAnchor constraintEqualToAnchor:contentView.bottomAnchor],
    [self.statusLabel.centerXAnchor constraintEqualToAnchor:contentView.centerXAnchor],
    [self.statusLabel.centerYAnchor constraintEqualToAnchor:contentView.centerYAnchor],
    [self.statusLabel.leadingAnchor constraintGreaterThanOrEqualToAnchor:contentView.leadingAnchor constant:32],
    [self.statusLabel.trailingAnchor constraintLessThanOrEqualToAnchor:contentView.trailingAnchor constant:-32]
  ]];

  self.window = [[NSWindow alloc]
    initWithContentRect:NSMakeRect(0, 0, 1100, 760)
              styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                backing:NSBackingStoreBuffered
                  defer:NO];
  self.window.title = @"Cochl.Sense Cloud Live Demo";
  self.window.contentView = contentView;
  self.window.delegate = self;
  [self.window center];
  [self.window makeKeyAndOrderFront:nil];
}

- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message {
  if (![message.name isEqualToString:@"windowMode"] ||
      ![message.body isKindOfClass:[NSDictionary class]]) {
    return;
  }
  id compactValue = ((NSDictionary *)message.body)[@"compact"];
  if (![compactValue respondsToSelector:@selector(boolValue)]) {
    return;
  }
  [self setCompactWindowEnabled:[compactValue boolValue]];
}

- (void)setCompactWindowEnabled:(BOOL)enabled {
  if (!self.window || self.compactWindow == enabled) {
    return;
  }

  if (enabled) {
    self.expandedWindowFrame = self.window.frame;
    self.expandedWindowMinSize = self.window.minSize;
    self.compactWindow = YES;
    self.window.minSize = NSMakeSize(360, 240);

    NSRect currentFrame = self.window.frame;
    NSRect targetFrame = [self.window frameRectForContentRect:NSMakeRect(0, 0, 400, 280)];
    targetFrame.origin.x = NSMinX(currentFrame);
    targetFrame.origin.y = NSMaxY(currentFrame) - NSHeight(targetFrame);

    NSScreen *screen = self.window.screen ?: NSScreen.mainScreen;
    if (screen) {
      NSRect visibleFrame = screen.visibleFrame;
      if (NSMinX(targetFrame) < NSMinX(visibleFrame)) {
        targetFrame.origin.x = NSMinX(visibleFrame);
      }
      if (NSMaxX(targetFrame) > NSMaxX(visibleFrame)) {
        targetFrame.origin.x = NSMaxX(visibleFrame) - NSWidth(targetFrame);
      }
      if (NSMinY(targetFrame) < NSMinY(visibleFrame)) {
        targetFrame.origin.y = NSMinY(visibleFrame);
      }
      if (NSMaxY(targetFrame) > NSMaxY(visibleFrame)) {
        targetFrame.origin.y = NSMaxY(visibleFrame) - NSHeight(targetFrame);
      }
    }
    [self.window setFrame:targetFrame display:YES animate:YES];
    return;
  }

  self.compactWindow = NO;
  self.window.minSize = self.expandedWindowMinSize;
  if (!NSEqualRects(self.expandedWindowFrame, NSZeroRect)) {
    [self.window setFrame:self.expandedWindowFrame display:YES animate:YES];
  }
}

- (void)startServer {
  NSString *runnerPath = [[self.projectRoot URLByAppendingPathComponent:@"scripts/run-macos-server.sh"] path];
  if (![[NSFileManager defaultManager] isExecutableFileAtPath:runnerPath]) {
    [self showStatus:@"scripts/run-macos-server.sh를 찾을 수 없거나 실행할 수 없습니다."];
    return;
  }

  NSString *script = @"exec \"$COCHL_SENSE_CLOUD_LIVE_DEMO_ROOT/scripts/run-macos-server.sh\"\n";
  NSTask *task = [[NSTask alloc] init];
  task.launchPath = @"/bin/zsh";
  task.arguments = @[@"-lc", script];

  NSMutableDictionary *environment = [[[NSProcessInfo processInfo] environment] mutableCopy];
  environment[@"COCHL_SENSE_CLOUD_LIVE_DEMO_ROOT"] = self.projectRoot.path;
  task.environment = environment;

  NSPipe *outputPipe = [NSPipe pipe];
  task.standardOutput = outputPipe;
  task.standardError = outputPipe;

  NSFileHandle *readHandle = outputPipe.fileHandleForReading;
  __weak typeof(self) weakSelf = self;
  readHandle.readabilityHandler = ^(NSFileHandle *handle) {
    NSData *data = handle.availableData;
    if (data.length == 0) {
      return;
    }
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (!text) {
      return;
    }
    dispatch_async(dispatch_get_main_queue(), ^{
      [weakSelf handleServerOutput:text];
    });
  };

  task.terminationHandler = ^(NSTask *finishedTask) {
    readHandle.readabilityHandler = nil;
    dispatch_async(dispatch_get_main_queue(), ^{
      AppDelegate *strongSelf = weakSelf;
      if (!strongSelf || strongSelf.isQuitting) {
        return;
      }
      [strongSelf showStatus:[NSString stringWithFormat:@"Cochl.Sense Cloud Live Demo 서버가 종료되었습니다: %d", finishedTask.terminationStatus]];
    });
  };

  NSError *error = nil;
  if (![task launchAndReturnError:&error]) {
    [self showStatus:[NSString stringWithFormat:@"Cochl.Sense Cloud Live Demo를 시작할 수 없습니다: %@", error.localizedDescription]];
    return;
  }
  self.serverTask = task;
}

- (void)handleServerOutput:(NSString *)text {
  [self.outputBuffer appendString:text];

  NSRange newlineRange = [self.outputBuffer rangeOfString:@"\n"];
  while (newlineRange.location != NSNotFound) {
    NSString *line = [[self.outputBuffer substringToIndex:newlineRange.location]
      stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    [self.outputBuffer deleteCharactersInRange:NSMakeRange(0, newlineRange.location + newlineRange.length)];
    [self handleServerLine:line];
    newlineRange = [self.outputBuffer rangeOfString:@"\n"];
  }
}

- (void)handleServerLine:(NSString *)line {
  NSString *prefix = @"Cochl.Sense Cloud Live Demo is running at ";
  if ([line hasPrefix:prefix]) {
    NSString *urlText = [line substringFromIndex:prefix.length];
    NSURL *url = [NSURL URLWithString:urlText];
    if (url) {
      self.serverScheme = url.scheme;
      self.serverHost = url.host;
      self.serverPort = url.port ? url.port.integerValue : 80;
      [self showStatus:@"Cochl.Sense Cloud Live Demo를 여는 중..."];
      [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
    }
  } else if ([line hasPrefix:@"Cochl.Sense Cloud Live Demo error:"]) {
    [self showStatus:line];
  }
}

- (void)showStatus:(NSString *)message {
  self.webView.hidden = YES;
  self.statusLabel.hidden = NO;
  self.statusLabel.stringValue = message ?: @"";
}

- (void)stopServer {
  NSTask *task = self.serverTask;
  if (!task || !task.isRunning) {
    return;
  }

  [task terminate];
  pid_t pid = task.processIdentifier;
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2 * NSEC_PER_SEC)), dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
    if (task.isRunning) {
      kill(pid, SIGKILL);
    }
  });
}

@end

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    NSApplication *application = [NSApplication sharedApplication];
    AppDelegate *delegate = [[AppDelegate alloc] init];
    application.delegate = delegate;
    [application run];
  }
  return 0;
}
