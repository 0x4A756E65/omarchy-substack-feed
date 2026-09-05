import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root

  moduleName: "0x4a756e65.omarchy-substack"
  ipcTarget: "0x4a756e65.omarchy-substack"
  manageIpc: false

  readonly property string backendPath: Qt.resolvedUrl("substack_backend.py").toString().replace(/^file:\/\//, "")
  readonly property int maxSnapshotCharacters: 2000000
  readonly property int maxSubscriptions: 512
  readonly property int maxArticles: 160

  property var feedState: ({
    status: "starting",
    message: "Starting Substack…",
    authenticated: false,
    syncing: false,
    subscriptions: [],
    articles: [],
    unread_count: 0,
    last_error: ""
  })
  property bool stateLoaded: false
  property string localError: ""
  property string authError: ""
  property bool settingsOpen: false
  property bool logoutArmed: false

  readonly property var subscriptions: Array.isArray(feedState.subscriptions) ? feedState.subscriptions : []
  readonly property var articles: Array.isArray(feedState.articles) ? feedState.articles : []
  readonly property var articleList: articles.length > 1 ? articles.slice(1, 60) : []
  readonly property var featured: articles.length > 0 ? articles[0] : null
  readonly property var newestUnread: {
    for (var i = 0; i < articles.length; i++) {
      if (articles[i].unread === true) return articles[i]
    }
    return null
  }
  readonly property int unreadCount: Number(feedState.unread_count || 0)
  readonly property bool authenticated: feedState.authenticated === true
  readonly property bool hasFeed: subscriptions.length > 0 || articles.length > 0
  // First launch should never flash the normal feed chrome or a reconnect
  // warning while the service creates its first snapshot.
  readonly property bool showOnboarding: !hasFeed && !authenticated
  readonly property bool showTicker: setting("showTicker", true) !== false
  readonly property bool notifyEnabled: setting("notify", true) !== false
  readonly property bool includeOwned: setting("includeOwned", false) === true
  readonly property bool showPublicationLogos: setting("showPublicationLogos", true) !== false
  readonly property int maxLabelWidth: Math.max(100, Math.min(420, Number(setting("maxLabelWidth", 210)) || 210))

  readonly property color orange: "#ff6719"
  // `foreground` is the static theme text color, used for panel/popup content
  // that sits on an opaque surface. Bar-facing text (glyph, ticker) instead
  // uses `barForeground` from the Panel base, which the host repoints to a
  // wallpaper-sampled contrasting color while the bar is transparent.
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.42)
  readonly property color dimmer: Qt.darker(foreground, 1.75)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property int barSize: bar ? bar.barSize : Style.bar.sizeHorizontal
  readonly property string tickerText: {
    if (!stateLoaded) return "Substack"
    if (feedState.syncing === true) return "Syncing…"
    if (unreadCount > 0 && showTicker && newestUnread) return String(newestUnread.title || "New post")
    if (showTicker && featured) return String(featured.title || "Substack")
    if (unreadCount > 0) return String(unreadCount)
    return "Substack"
  }

  function boundedString(value, limit, fallback) {
    return typeof value === "string" && value.length <= limit ? value : (fallback || "")
  }

  function boundedNumber(value) {
    return typeof value === "number" && isFinite(value) && value >= 0 ? value : 0
  }

  function boundedHttpsUrl(value) {
    var source = boundedString(value, 4096, "")
    return /^https:\/\/[^\s/@]+(?::443)?(?:\/|$)/i.test(source) ? source : ""
  }

  function normalizeSnapshot(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== 1) return null
    if (!Array.isArray(value.subscriptions) || value.subscriptions.length > maxSubscriptions) return null
    if (!Array.isArray(value.articles) || value.articles.length > maxArticles) return null

    var publications = []
    var publicationIds = Object.create(null)
    for (var i = 0; i < value.subscriptions.length; i++) {
      var sourcePublication = value.subscriptions[i]
      if (!sourcePublication || typeof sourcePublication !== "object" || Array.isArray(sourcePublication)) continue
      var publicationId = boundedString(sourcePublication.id, 63, "").toLowerCase()
      if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(publicationId) || publicationIds[publicationId]) continue
      publicationIds[publicationId] = true
      publications.push({
        id: publicationId,
        name: boundedString(sourcePublication.name, 220, publicationId),
        logo_url: boundedHttpsUrl(sourcePublication.logo_url)
      })
    }

    var stories = []
    var storyIds = Object.create(null)
    for (var j = 0; j < value.articles.length; j++) {
      var sourceArticle = value.articles[j]
      if (!sourceArticle || typeof sourceArticle !== "object" || Array.isArray(sourceArticle)) continue
      var articleId = boundedString(sourceArticle.id, 24, "")
      var articlePublicationId = boundedString(sourceArticle.publication_id, 63, "").toLowerCase()
      if (!/^[0-9a-f]{24}$/.test(articleId) || storyIds[articleId] || !publicationIds[articlePublicationId]) continue
      storyIds[articleId] = true
      stories.push({
        id: articleId,
        publication_id: articlePublicationId,
        publication: boundedString(sourceArticle.publication, 220, articlePublicationId),
        author: boundedString(sourceArticle.author, 120, ""),
        title: boundedString(sourceArticle.title, 220, "Untitled post"),
        link: boundedHttpsUrl(sourceArticle.link),
        published: boundedString(sourceArticle.published, 64, ""),
        published_ts: boundedNumber(sourceArticle.published_ts),
        excerpt: boundedString(sourceArticle.excerpt, 280, ""),
        image_url: boundedHttpsUrl(sourceArticle.image_url),
        publication_logo_url: boundedHttpsUrl(sourceArticle.publication_logo_url),
        unread: sourceArticle.unread === true
      })
    }

    var sourceAccount = value.account && typeof value.account === "object" && !Array.isArray(value.account)
      ? value.account : ({})
    var allowedStatuses = ["starting", "syncing", "ready", "error", "expired", "disconnected"]
    var status = boundedString(value.status, 32, "starting")
    if (allowedStatuses.indexOf(status) < 0) status = "starting"
    return {
      schema: 1,
      status: status,
      message: boundedString(value.message, 240, "Starting Substack…"),
      authenticated: value.authenticated === true,
      syncing: value.syncing === true,
      account: {
        name: boundedString(sourceAccount.name, 160, ""),
        handle: boundedString(sourceAccount.handle, 80, ""),
        photo_url: boundedHttpsUrl(sourceAccount.photo_url)
      },
      subscriptions: publications,
      articles: stories,
      unread_count: stories.reduce(function(total, article) { return total + (article.unread ? 1 : 0) }, 0),
      last_sync: boundedString(value.last_sync, 64, ""),
      last_error: boundedString(value.last_error, 240, "")
    }
  }

  function parseState(content) {
    var source = String(content || "")
    if (source.length > maxSnapshotCharacters) {
      localError = "The local feed snapshot exceeded its safety limit"
      return
    }
    try {
      var value = normalizeSnapshot(JSON.parse(source))
      if (!value) throw new Error("invalid schema")
      feedState = value
      stateLoaded = true
      localError = ""
    } catch (error) {
      localError = "The local feed snapshot is invalid"
    }
  }

  function loadSnapshot() {
    if (!stateProcess.running) stateProcess.running = true
  }

  function runBackend(args) {
    var command = ["python3", backendPath]
    for (var i = 0; i < args.length; i++) command.push(String(args[i]))
    Quickshell.execDetached(command)
  }

  function syncSettings() {
    runBackend(["config", "notify", notifyEnabled ? "true" : "false"])
    runBackend(["config", "include_owned", includeOwned ? "true" : "false"])
  }

  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) {
      if (values[key] === undefined) delete entry[key]
      else entry[key] = values[key]
    }
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function connectAccount() {
    if (authProcess.running) {
      authError = "A Substack sign-in window is already open"
      return
    }
    close()
    authError = ""
    authProcess.running = true
  }

  function refresh() {
    runBackend(["refresh"])
  }

  function showSettings(open) {
    settingsOpen = open === true
    logoutArmed = false
    logoutArmTimer.stop()
  }

  function disconnectAccount() {
    if (!logoutArmed) {
      logoutArmed = true
      logoutArmTimer.restart()
      return
    }
    logoutArmed = false
    settingsOpen = false
    runBackend(["disconnect"])
  }

  function openArticle(article) {
    if (!article || !article.id) return
    runBackend(["open", article.id])
  }

  function markAllRead() {
    runBackend(["mark-all-read"])
  }

  function timeAgo(value) {
    var stamp = Date.parse(String(value || ""))
    if (isNaN(stamp)) return "RECENT"
    var seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000))
    if (seconds < 60) return "NOW"
    if (seconds < 3600) return Math.floor(seconds / 60) + "M"
    if (seconds < 86400) return Math.floor(seconds / 3600) + "H"
    if (seconds < 604800) return Math.floor(seconds / 86400) + "D"
    var date = new Date(stamp)
    return date.toLocaleDateString(Qt.locale(), "MMM d").toUpperCase()
  }

  function syncedLabel(value) {
    var age = timeAgo(value)
    if (age === "RECENT") return "READY"
    if (age === "NOW") return "SYNCED NOW"
    if (/^\d+[MHD]$/.test(age)) return "SYNCED " + age + " AGO"
    return "SYNCED " + age
  }

  function publicationLogo(article) {
    if (!article) return ""
    if (article.publication_logo_url) return String(article.publication_logo_url)
    var id = String(article.publication_id || "")
    for (var i = 0; i < subscriptions.length; i++) {
      if (String(subscriptions[i].id || "") === id) return String(subscriptions[i].logo_url || "")
    }
    return ""
  }

  function fullDate(value) {
    var stamp = Date.parse(String(value || ""))
    if (isNaN(stamp)) return "Not synced yet"
    return new Date(stamp).toLocaleString(Qt.locale(), "MMM d · h:mm AP")
  }

  function triggerPress(button) {
    if (bar) bar.hideTooltip(root)
    if (button === Qt.MiddleButton) refresh()
    else if (button === Qt.RightButton && unreadCount > 0) markAllRead()
    else toggle()
  }

  onSettingsChanged: syncSettings()
  onOpenedChanged: if (!opened) showSettings(false)
  onShowOnboardingChanged: if (showOnboarding) showSettings(false)
  Component.onCompleted: {
    loadSnapshot()
    syncSettings()
  }

  Process {
    id: stateProcess
    command: ["python3", root.backendPath, "snapshot"]

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseState(text)
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = String(text || "").trim()
        if (message !== "") root.localError = "Waiting for the Substack service"
      }
    }
  }

  Process {
    id: authProcess
    command: ["python3", root.backendPath, "auth-window"]

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = String(text || "").trim()
        if (message !== "") root.authError = message.substring(0, 240)
      }
    }

    onExited: function(exitCode) {
      if (exitCode === 0) {
        root.authError = ""
        root.loadSnapshot()
      } else if (root.authError === "") {
        root.authError = exitCode === 3
          ? "A Substack sign-in window is already open"
          : "Substack sign-in closed before the account connected"
      }
    }
  }

  Timer {
    interval: 1500
    repeat: true
    running: true
    onTriggered: root.loadSnapshot()
  }

  Timer {
    id: logoutArmTimer
    interval: 5000
    repeat: false
    onTriggered: root.logoutArmed = false
  }

  // --------------------------------------------------------------- bar pill
  visible: true
  readonly property real tickerSlot: !vertical && tickerText !== "" ? tickerClip.width + Style.space(7) : 0
  implicitWidth: vertical ? barSize : Math.round(glyph.implicitWidth + tickerSlot + Style.space(14))
  implicitHeight: vertical ? Math.round(glyph.implicitHeight + Style.space(10)) : barSize

  Row {
    anchors.centerIn: parent
    spacing: root.vertical ? 0 : Style.space(7)

    Item {
      width: glyph.implicitWidth
      height: glyph.implicitHeight

      Text {
        id: glyph
        anchors.centerIn: parent
        text: "󰂺"
        color: root.unreadCount > 0 ? root.orange : root.barForeground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body

        Behavior on color {
          enabled: !root.bar || root.bar.foregroundAnimationEnabled
          ColorAnimation { duration: 160 }
        }
      }

      Rectangle {
        visible: root.unreadCount > 0
        width: Style.space(5)
        height: width
        radius: width / 2
        color: root.orange
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.rightMargin: -Style.space(2)
        anchors.topMargin: -Style.space(1)
      }
    }

    Item {
      id: tickerClip
      visible: !root.vertical && root.tickerText !== ""
      width: visible ? Math.min(root.maxLabelWidth, tickerLabel.implicitWidth) : 0
      height: glyph.height
      clip: true
      anchors.verticalCenter: parent.verticalCenter

      Text {
        id: tickerLabel
        text: root.tickerText
        textFormat: Text.PlainText
        color: root.barForeground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: root.unreadCount > 0 && !root.showTicker
        anchors.verticalCenter: parent.verticalCenter
        x: -panOffset

        property real panOffset: 0
        readonly property real overflow: Math.max(0, implicitWidth - tickerClip.width)
        onTextChanged: panOffset = 0

        Behavior on color {
          enabled: !root.bar || root.bar.foregroundAnimationEnabled
          ColorAnimation { duration: 160 }
        }
      }

      SequentialAnimation {
        running: root.showTicker && tickerLabel.overflow > 0 && !root.opened
        loops: Animation.Infinite
        onRunningChanged: if (!running) tickerLabel.panOffset = 0
        PauseAnimation { duration: 3000 }
        NumberAnimation {
          target: tickerLabel
          property: "panOffset"
          from: 0
          to: tickerLabel.overflow
          duration: Math.max(1600, tickerLabel.overflow * 42)
          easing.type: Easing.InOutQuad
        }
        PauseAnimation { duration: 1800 }
        NumberAnimation {
          target: tickerLabel
          property: "panOffset"
          from: tickerLabel.overflow
          to: 0
          duration: Math.max(900, tickerLabel.overflow * 22)
          easing.type: Easing.InOutQuad
        }
      }
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.RightButton | Qt.MiddleButton
    onClicked: function(mouse) { root.triggerPress(mouse.button) }
    onEntered: if (root.bar) root.bar.showTooltip(root,
      root.unreadCount > 0 ? root.unreadCount + " unread Substack post" + (root.unreadCount === 1 ? "" : "s") : "Substack feed")
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  // -------------------------------------------------------------- feed card
  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.opened
    contentWidth: popup.fittedContentWidth(Style.space(450))
    contentHeight: root.showOnboarding
      ? popup.fittedContentHeight(Style.space(340), Style.space(410))
      : popup.fittedContentHeight(Style.space(610), Style.space(680))

    Item {
      anchors.fill: parent

      // Onboarding is intentionally sparse: the official Substack page owns
      // credentials and this panel only explains what will happen.
      Item {
        id: onboarding
        anchors.fill: parent
        visible: root.showOnboarding

        Column {
          width: Math.min(parent.width, Style.space(350))
          anchors.centerIn: parent
          spacing: Style.space(16)

          Rectangle {
            width: Style.space(76)
            height: width
            radius: Style.space(22)
            color: root.orange
            anchors.horizontalCenter: parent.horizontalCenter

            Text {
              anchors.centerIn: parent
              text: "󰂺"
              color: "white"
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }

          Text {
            width: parent.width
            text: "Your newsletters, at a glance."
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            text: "Connect once to see your free and paid subscriptions, get notified when something lands, and open every story in your browser. Use a password or an email code from Substack."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            lineHeight: 1.15
          }

          Button {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "Connect Substack"
            iconText: "󰌾"
            foreground: root.foreground
            accent: root.orange
            background: Style.selectedFillFor(root.foreground, root.orange)
            bordered: true
            horizontalPadding: Style.space(18)
            verticalPadding: Style.space(9)
            onClicked: root.connectAccount()
          }

          Text {
            width: parent.width
            text: "You’ll sign in on Substack’s own website. The session stays in your desktop keyring."
            color: root.dimmer
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            visible: root.authError !== "" || root.localError !== ""
            text: root.authError !== "" ? root.authError : root.localError
            textFormat: Text.PlainText
            color: "#ff7b72"
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }
        }
      }

      Item {
        id: feedView
        anchors.fill: parent
        visible: !root.showOnboarding && !root.settingsOpen

        Row {
          id: header
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: parent.top
          height: Style.space(54)
          spacing: Style.space(12)

          Rectangle {
            width: Style.space(40)
            height: width
            radius: Style.space(12)
            color: root.orange
            anchors.verticalCenter: parent.verticalCenter

            Text {
              anchors.centerIn: parent
              text: "󰂺"
              color: "white"
              font.family: root.fontFamily
              font.pixelSize: Style.font.iconLarge
            }
          }

          Column {
            width: parent.width - Style.space(40) - headerActions.width - Style.space(24)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            Text {
              width: parent.width
              text: "THE READING DESK"
              color: root.orange
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.3
            }

            Text {
              width: parent.width
              text: root.feedState.message || "Your Substack feed"
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
          }

          Row {
            id: headerActions
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Button {
              iconText: "󰒓"
              tooltipText: "Substack settings"
              foreground: root.foreground
              accent: root.orange
              onClicked: root.showSettings(true)
            }

            Button {
              iconText: "󰑐"
              tooltipText: "Refresh subscriptions and feeds"
              foreground: root.foreground
              accent: root.orange
              iconSpinning: root.feedState.syncing === true
              onClicked: root.refresh()
            }
          }
        }

        BorderSurface {
          id: reconnectBanner
          visible: root.hasFeed && !root.authenticated
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: header.bottom
          anchors.topMargin: Style.space(8)
          height: visible ? Style.space(42) : 0
          radius: Style.spacing.labelGap
          color: Qt.rgba(root.orange.r, root.orange.g, root.orange.b, 0.10)
          borderSpec: Border.controlSpec("normal", root.foreground, root.orange)

          Row {
            anchors.fill: parent
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(8)
            spacing: Style.space(8)

            Text {
              text: "󰌾"
              color: root.orange
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              width: parent.width - reconnectButton.width - Style.space(34)
              text: "Reconnect to keep subscriptions in sync"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
              anchors.verticalCenter: parent.verticalCenter
            }

            Button {
              id: reconnectButton
              text: "Reconnect"
              foreground: root.foreground
              accent: root.orange
              horizontalPadding: Style.space(8)
              verticalPadding: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              onClicked: root.connectAccount()
            }
          }
        }

        BorderSurface {
          id: featuredCard
          visible: root.featured !== null
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: reconnectBanner.visible ? reconnectBanner.bottom : header.bottom
          anchors.topMargin: Style.space(12)
          height: visible ? Style.space(146) : 0
          radius: Style.space(14)
          color: heroMouse.containsMouse
            ? Style.hoverFillFor(root.foreground, root.orange)
            : Qt.rgba(root.orange.r, root.orange.g, root.orange.b, 0.08)
          borderSpec: Border.controlSpec(heroMouse.containsMouse ? "hover-cursor" : "normal", root.foreground, root.orange)

          Behavior on color { ColorAnimation { duration: 120 } }

          Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: Style.space(4)
            color: root.featured && root.featured.unread ? root.orange : "transparent"
            radius: width / 2
          }

          Column {
            anchors.fill: parent
            anchors.margins: Style.space(15)
            anchors.leftMargin: Style.space(18)
            spacing: Style.space(7)

            Row {
              width: parent.width
              spacing: Style.space(8)

              Item {
                id: featuredMark
                property string logoSource: root.publicationLogo(root.featured)
                visible: root.showPublicationLogos && logoSource !== "" && featuredMarkImage.status !== Image.Error
                width: visible ? Style.space(27) : 0
                height: Style.space(27)

                Rectangle {
                  anchors.fill: parent
                  radius: width / 2
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
                  clip: true

                  Image {
                    id: featuredMarkImage
                    anchors.fill: parent
                    source: featuredMark.logoSource
                    sourceSize.width: Style.space(54)
                    sourceSize.height: Style.space(54)
                    asynchronous: true
                    cache: true
                    fillMode: Image.PreserveAspectCrop
                  }
                }
              }

              Text {
                width: parent.width - featuredMark.width - featuredTime.width - parent.spacing * 2
                text: root.featured ? String(root.featured.publication || "Substack") : "Substack"
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                elide: Text.ElideRight
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: featuredTime
                text: root.featured ? root.timeAgo(root.featured.published) : ""
                color: root.orange
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Text {
              width: parent.width
              text: root.featured ? String(root.featured.title || "") : ""
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              text: root.featured ? String(root.featured.excerpt || "") : ""
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
              visible: text !== ""
            }
          }

          MouseArea {
            id: heroMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.openArticle(root.featured)
          }
        }

        Item {
          id: latestHeader
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: featuredCard.visible ? featuredCard.bottom : (reconnectBanner.visible ? reconnectBanner.bottom : header.bottom)
          anchors.topMargin: Style.space(15)
          height: Style.space(28)

          Text {
            text: "LATEST"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            anchors.verticalCenter: parent.verticalCenter
          }

          Row {
            visible: root.unreadCount > 0
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            Text {
              text: root.unreadCount + " NEW"
              color: root.orange
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              anchors.verticalCenter: parent.verticalCenter
            }

            Button {
              iconText: "󰄬"
              tooltipText: "Mark all as read"
              foreground: root.foreground
              accent: root.orange
              horizontalPadding: Style.space(7)
              verticalPadding: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              onClicked: root.markAllRead()
            }
          }
        }

        ListView {
          id: articleListView
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: latestHeader.bottom
          anchors.topMargin: Style.space(3)
          anchors.bottom: footer.top
          anchors.bottomMargin: Style.space(8)
          clip: true
          spacing: Style.space(3)
          model: root.articleList
          boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar {
            id: articleScrollBar
            width: Style.space(5)
            policy: articleListView.contentHeight > articleListView.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
          }

          delegate: BorderSurface {
            id: articleRow
            required property var modelData
            // Attached scrollbars overlay flickable content. Reserve a fixed
            // gutter so dates and hover borders never sit underneath it.
            width: articleListView.width - Style.space(10)
            height: Style.space(78)
            radius: Style.spacing.labelGap
            color: rowMouse.containsMouse ? Style.hoverFillFor(root.foreground, root.orange) : "transparent"
            borderSpec: rowMouse.containsMouse
              ? Border.controlSpec("hover-cursor", root.foreground, root.orange)
              : Border.none()

            Behavior on color { ColorAnimation { duration: 100 } }

            Rectangle {
              visible: articleRow.modelData.unread === true
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(3)
              height: Style.space(34)
              radius: width / 2
              color: root.orange
            }

            Item {
              id: publicationMark
              property string logoSource: root.publicationLogo(articleRow.modelData)
              visible: root.showPublicationLogos && logoSource !== "" && publicationImage.status !== Image.Error
              width: visible ? Style.space(35) : 0
              height: Style.space(35)
              anchors.left: parent.left
              anchors.leftMargin: Style.space(11)
              anchors.verticalCenter: parent.verticalCenter

              Rectangle {
                anchors.fill: parent
                radius: Style.space(11)
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
                clip: true

                Image {
                  id: publicationImage
                  anchors.fill: parent
                  source: publicationMark.logoSource
                  sourceSize.width: Style.space(70)
                  sourceSize.height: Style.space(70)
                  asynchronous: true
                  cache: true
                  fillMode: Image.PreserveAspectCrop
                }
              }
            }

            Column {
              anchors.left: publicationMark.visible ? publicationMark.right : parent.left
              anchors.leftMargin: Style.space(11)
              anchors.right: rowTime.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(4)

              Text {
                width: parent.width
                text: String(articleRow.modelData.title || "Untitled")
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: articleRow.modelData.unread === true
                maximumLineCount: 2
                elide: Text.ElideRight
                wrapMode: Text.WordWrap
              }

              Text {
                width: parent.width
                text: String(articleRow.modelData.publication || "Substack")
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            Text {
              id: rowTime
              anchors.right: parent.right
              anchors.rightMargin: Style.space(10)
              anchors.top: parent.top
              anchors.topMargin: Style.space(12)
              text: root.timeAgo(articleRow.modelData.published)
              color: articleRow.modelData.unread === true ? root.orange : root.dimmer
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: articleRow.modelData.unread === true
            }

            MouseArea {
              id: rowMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.openArticle(articleRow.modelData)
            }
          }

          Text {
            anchors.centerIn: parent
            visible: root.articleList.length === 0 && root.featured === null
            text: root.feedState.syncing === true ? "Gathering your latest posts…" : "No posts yet"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
        }

        Item {
          id: footer
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          height: Style.space(29)

          Rectangle {
            id: syncDot
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(6)
            height: width
            radius: width / 2
            color: root.feedState.status === "ready" ? "#49c978"
              : root.feedState.syncing === true ? root.orange
              : root.feedState.status === "error" || root.feedState.status === "expired" ? "#ff7b72"
              : root.dim
          }

          Text {
            anchors.left: syncDot.right
            anchors.leftMargin: Style.space(7)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            text: {
              if (root.authError !== "") return root.authError
              if (root.localError !== "") return root.localError
              if (root.feedState.last_error) return String(root.feedState.last_error)
              if (root.feedState.syncing === true) return "SYNCING"
              return root.syncedLabel(root.feedState.last_sync)
            }
            color: root.dimmer
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
            textFormat: Text.PlainText
          }
        }
      }

      Item {
        id: settingsPage
        anchors.fill: parent
        visible: !root.showOnboarding && root.settingsOpen
        Keys.onEscapePressed: function(event) {
          root.showSettings(false)
          event.accepted = true
        }

        Row {
          id: settingsHeader
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: parent.top
          height: Style.space(54)
          spacing: Style.space(10)

          Button {
            iconText: "󰁍"
            tooltipText: "Back to reading desk"
            foreground: root.foreground
            accent: root.orange
            anchors.verticalCenter: parent.verticalCenter
            onClicked: root.showSettings(false)
          }

          Column {
            width: parent.width - Style.space(54)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            Text {
              width: parent.width
              text: "SETTINGS"
              color: root.orange
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.3
            }

            Text {
              width: parent.width
              text: "Reading desk preferences and account"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
          }
        }

        Rectangle {
          id: settingsDivider
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: settingsHeader.bottom
          height: 1
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
        }

        Flickable {
          id: settingsFlick
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: settingsDivider.bottom
          anchors.topMargin: Style.space(14)
          anchors.bottom: parent.bottom
          contentWidth: settingsContent.width
          contentHeight: settingsContent.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.VerticalFlick
          interactive: contentHeight > height
          ScrollBar.vertical: ScrollBar {
            id: settingsScrollBar
            width: Style.space(5)
            policy: ScrollBar.AsNeeded
          }

          Column {
            id: settingsContent
            // Keep toggles, buttons, and card borders out from under the
            // overlay scrollbar at every scroll position.
            width: settingsFlick.width - Style.space(12)
            spacing: Style.space(16)

            BorderSurface {
              width: parent.width
              height: Style.space(78)
              radius: Style.space(12)
              color: Qt.rgba(root.orange.r, root.orange.g, root.orange.b, 0.07)
              borderSpec: Border.controlSpec("normal", root.foreground, root.orange)

              Column {
                anchors.fill: parent
                anchors.margins: Style.space(13)
                spacing: Style.space(5)

                Text {
                  width: parent.width
                  text: root.feedState.account && root.feedState.account.handle
                    ? "@" + String(root.feedState.account.handle)
                    : "SUBSTACK ACCOUNT"
                  textFormat: Text.PlainText
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  text: root.subscriptions.length + " reading publication" + (root.subscriptions.length === 1 ? "" : "s")
                    + "  ·  Synced " + root.fullDate(root.feedState.last_sync)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }
            }

            Text {
              text: "EXPERIENCE"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.1
            }

            Toggle {
              width: parent.width
              label: "Publication artwork"
              description: "Use real publication logos when Substack provides one; never generate placeholder initials."
              checked: root.showPublicationLogos
              foreground: root.foreground
              accent: root.orange
              fontFamily: root.fontFamily
              onClicked: root.persistSettings({ showPublicationLogos: !root.showPublicationLogos })
            }

            Toggle {
              width: parent.width
              label: "Substack titles in bar"
              description: "Show and scroll the newest Substack title, prioritizing unread posts. Off shows only its unread count; music scrolling is controlled by Spotmarchy."
              checked: root.showTicker
              foreground: root.foreground
              accent: root.orange
              fontFamily: root.fontFamily
              onClicked: root.persistSettings({ showTicker: !root.showTicker })
            }

            Toggle {
              width: parent.width
              label: "Desktop notifications"
              description: "Notify for genuinely new posts after the initial feed has been seeded."
              checked: root.notifyEnabled
              foreground: root.foreground
              accent: root.orange
              fontFamily: root.fontFamily
              onClicked: root.persistSettings({ notify: !root.notifyEnabled })
            }

            Toggle {
              width: parent.width
              label: "Include my publications"
              description: "Show publications you administer alongside newsletters you read. Off by default."
              checked: root.includeOwned
              foreground: root.foreground
              accent: root.orange
              fontFamily: root.fontFamily
              onClicked: root.persistSettings({ includeOwned: !root.includeOwned })
            }

            Text {
              text: "SYNC & ACCOUNT"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.1
            }

            Row {
              width: parent.width
              spacing: Style.space(8)

              Button {
                width: (parent.width - parent.spacing) / 2
                text: root.feedState.syncing === true ? "Syncing…" : "Resync now"
                iconText: "󰑐"
                iconSpinning: root.feedState.syncing === true
                enabled: root.feedState.syncing !== true
                foreground: root.foreground
                accent: root.orange
                bordered: true
                onClicked: root.refresh()
              }

              Button {
                width: (parent.width - parent.spacing) / 2
                text: "Reconnect"
                iconText: "󰌾"
                foreground: root.foreground
                accent: root.orange
                bordered: true
                onClicked: root.connectAccount()
              }
            }

            Button {
              width: parent.width
              text: root.logoutArmed ? "Confirm log out & clear feed" : "Log out"
              iconText: root.logoutArmed ? "󰅖" : "󰍃"
              foreground: root.logoutArmed ? "#ff7b72" : root.foreground
              accent: "#ff7b72"
              bordered: true
              onClicked: root.disconnectAccount()
            }

            Text {
              width: parent.width
              text: "Logging out removes the Substack session from your desktop keyring and clears the local feed. Your Substack subscriptions are never changed."
              color: root.dimmer
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Item { width: 1; height: Style.space(4) }
          }
        }
      }
    }
  }

}
