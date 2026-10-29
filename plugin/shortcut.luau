--!nonstrict
local client = require("./client.luau")
shortcut.setLabel("Switch operating system")
shortcut.setIcon("device-desktop")
shortcut.setEnabled(client.enabled())
function onClick()
    if client.enabled() then noctalia.togglePanel("local/switch-os:switcher") end
end
