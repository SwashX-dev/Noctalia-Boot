--!nonstrict
local client = require("./client.luau")
barWidget.setGlyph("device-desktop")
barWidget.setTooltip("Noctalia Boot · asks before restarting")
function onClick()
    if client.enabled() then noctalia.togglePanel("local/switch-os:switcher") end
end
