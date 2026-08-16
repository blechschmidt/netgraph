// The whole client. A language server that needs more than this from its client
// is a language server that has put logic in the wrong place: everything
// netviz knows is in `netviz lsp`, and this only has to start it and hand it
// the YAML files.
//
// Configuration lives in package.json; docs/lsp.md is the setup guide.

const { workspace, window, commands } = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

/** @type {import('vscode-languageclient/node').LanguageClient | undefined} */
let client;

/** @type {import('vscode').OutputChannel | undefined} */
let channel;

function settings() {
  return workspace.getConfiguration("netviz");
}

async function start() {
  if (!settings().get("server.enable", true)) {
    return;
  }
  const command = settings().get("server.path", "netviz");
  const args = settings().get("server.args", ["lsp"]);
  client = new LanguageClient(
    "netviz",
    "netviz",
    {
      run: { command, args, transport: TransportKind.stdio },
      debug: { command, args, transport: TransportKind.stdio },
    },
    {
      // Every YAML file: which of them are inventory documents is the loader's
      // decision, not the extension's, and a file that is not one simply
      // produces no diagnostics.
      documentSelector: [{ scheme: "file", language: "yaml" }],
      synchronize: {
        // netviz watches the folder itself, but a client that is already
        // watching may as well say so: the server treats both as one signal.
        fileEvents: workspace.createFileSystemWatcher(
          "**/{*.yaml,*.yml,netviz.toml,.netvizignore}",
        ),
      },
      outputChannel: channel,
    },
  );
  try {
    await client.start();
  } catch (error) {
    client = undefined;
    window.showErrorMessage(
      `netviz: could not start '${command} ${args.join(" ")}'. ` +
        "Set netviz.server.path to the executable, or see docs/lsp.md. " +
        `(${error})`,
    );
  }
}

async function stop() {
  const running = client;
  client = undefined;
  if (running) {
    await running.stop();
  }
}

async function activate(context) {
  channel = window.createOutputChannel("netviz");
  context.subscriptions.push(channel);
  context.subscriptions.push(
    commands.registerCommand("netviz.restart", async () => {
      await stop();
      await start();
    }),
  );
  context.subscriptions.push(
    workspace.onDidChangeConfiguration(async (event) => {
      if (event.affectsConfiguration("netviz.server")) {
        await stop();
        await start();
      }
    }),
  );
  await start();
}

module.exports = { activate, deactivate: stop };
