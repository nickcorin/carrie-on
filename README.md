# Carrie On

Resume your local Codex chats after your usage quota resets, from your Mac's menu bar.

Choose the chats you want to continue and add instructions for each. Carrie On resumes the work when quota is available after a reset. Manage your queue from the menu bar or CLI.

## Install

You need an Apple Silicon Mac, the Codex desktop app, and a signed-in Codex CLI.

1. Download the DMG from [GitHub Releases](https://github.com/nickcorin/carrie-on/releases).
2. Open it and drag **Carrie On** onto **Applications**. You can also install directly in `~/Applications`.
3. Eject the disk image and open the installed app.
4. If prompted, choose your Codex executable. Run `command -v codex` in Terminal to find it.
5. Open Carrie On from the menu bar, choose a chat, and add it to your queue.

The app is not notarized. If macOS blocks it, open **System Settings**, select **Privacy & Security**, and choose **Open Anyway** for Carrie On. Runtime compatibility with macOS 13 has not been verified.

Keep Codex open and your Mac awake and connected while waiting for work to resume. If setup fails, follow the error dialog and choose **Retry**.

## Uninstall

Remove the background services before deleting the app:

```sh
"/Applications/Carrie On.app/Contents/MacOS/carrie_on" remove-installation --yes
```

Adjust the path if you installed in `~/Applications`. Quit Carrie On and move it to Trash. Saved data remains in `~/Library/Application Support/Carrie On`. To back it up, quit the app and copy that folder. Delete the folder only if you also want to discard your data.

## Documentation

- [Changelog](CHANGELOG.md)
- [Security and vulnerability reporting](SECURITY.md)

Report bugs through [GitHub Issues](https://github.com/nickcorin/carrie-on/issues). Carrie On is an independent project, not an official OpenAI product.

## Releasing

Releasing requires GitHub CLI and configured Git signing. Set `VERSION`, add a matching changelog entry, and commit the changes. Run `just release` from a clean checkout to run checks, push a signed version tag, and create a draft GitHub release. Complete [package acceptance](docs/features/distribution.md#package-acceptance), then run `just publish` to publish the draft. Use a new version for each release.

## License

[MIT](LICENSE), copyright Nicholas Corin. Bundled third-party components retain their own licenses, included inside the app.
