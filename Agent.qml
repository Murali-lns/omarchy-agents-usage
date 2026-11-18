import QtQuick

// One agent's usage record. The text arrives from Main.qml's bounded record
// scan (sync-scan.sh), never from reading the file here, so a large or
// growing record cannot be loaded whole into the shell. The panel never
// learns how the numbers were made: a record that appears in the usage
// directory is an agent, whoever wrote it.
Item {
  id: root
  visible: false

  property string agentId: ""
  property string path: ""
  property string rawText: ""
  property var record: null

  // A string property only signals when its value changes, so an unchanged
  // record is not re-parsed on every periodic scan.
  onRawTextChanged: parse(rawText)
  Component.onCompleted: parse(rawText)

  function parse(content) {
    if (String(content || "") === "") {
      root.record = null
      return
    }
    try {
      var parsed = JSON.parse(String(content))
      root.record = parsed && typeof parsed === "object" ? parsed : null
    } catch (e) {
      console.warn("agents", "Ignoring bad usage record", root.agentId, String(e).slice(0, 200))
      root.record = null
    }
  }
}
