import json

import typer

from ancs4linux.common.apis import AdvertisingAPI
from ancs4linux.common.url_config import load_config, remove_mapping, resolve_target, set_mapping

advertising_api: AdvertisingAPI
app = typer.Typer()


@app.command()
def get_all_hci() -> None:
    """Get all HCI supporting Bluetooth Low Energy."""
    print(json.dumps(advertising_api.GetAllHci()))


@app.command()
def enable_advertising(
    hci_address: str = typer.Option(..., help="Address of device to advertise on"),
    name: str = typer.Option("ancs4linux", help="Name to advertise on"),
) -> None:
    """Enable advertising and pairing."""
    advertising_api.EnableAdvertising(hci_address, name)


@app.command()
def disable_advertising(
    hci_address: str = typer.Option(..., help="Address of device to advertise on"),
) -> None:
    """Disable advertising and, if enabled automatically, pairing."""
    advertising_api.DisableAdvertising(hci_address)


@app.command()
def enable_pairing() -> None:
    """Enable just pairing."""
    advertising_api.EnablePairing()


@app.command()
def disable_pairing() -> None:
    """Disable just pairing."""
    advertising_api.DisablePairing()


@app.command()
def set_url(
    key: str = typer.Option(..., help="App bundle ID (e.g. com.microsoft.Outlook) or app name (e.g. Outlook)"),
    target: str = typer.Option(..., help="URL (https://...) or Linux app command prefixed with app:// (e.g. app://spotify)"),
) -> None:
    """Set a custom URL or app to open for a notification source."""
    set_mapping(key, target)
    print(f"Set: {key} -> {target}")


@app.command()
def remove_url(
    key: str = typer.Option(..., help="App bundle ID or app name to remove"),
) -> None:
    """Remove a custom URL or app mapping."""
    if remove_mapping(key):
        print(f"Removed mapping for: {key}")
    else:
        print(f"No mapping found for: {key}")
        raise typer.Exit(1)


@app.command()
def list_urls() -> None:
    """List all configured URL and app mappings."""
    config = load_config()
    if not config:
        print("No custom mappings configured.")
        return
    for key, target in config.items():
        print(f"  {key} -> {target}")


@app.command()
def resolve_url(
    app_id: str = typer.Option(..., help="App bundle ID"),
    app_name: str = typer.Option(..., help="App name"),
) -> None:
    """Show what URL or app would be opened for a given app (for debugging)."""
    print(resolve_target(app_id, app_name))


@app.callback()
def main(
    advertising_dbus: str = typer.Option(
        "ancs4linux.Advertising", help="Advertising servive path"
    )
) -> None:
    """Issue commands to ancs4linux servers running in background."""
    global advertising_api
    advertising_api = AdvertisingAPI.connect(advertising_dbus)
