-- ============================================================
-- SERIAL NETWORK SWITCH — Retro Gadgets
-- ============================================================
-- Build requirements for this gadget:
--   4x Serial      (gdt.Serial0 .. gdt.Serial3)
--   4x Led         (gdt.Led0 .. gdt.Led3)          -- activity light per port
--   1x Screen      (gdt.Screen0)
--   1x VideoChip   (gdt.VideoChip0)
--   1x ROM chip    (for gdt.ROM.System.SpriteSheets["StandardFont"])
--
-- IN-GAME WIRING (done by hand in the Multitool, not in code):
--   Wire Serial0 -> CPU event channel 1
--   Wire Serial1 -> CPU event channel 2
--   Wire Serial2 -> CPU event channel 3
--   Wire Serial3 -> CPU event channel 4
--
-- Each Serial module's `Port` property must be set (in code below,
-- or by editing the property directly) to match one end of a virtual
-- null-modem COM pair created with com0com (Windows) or socat
-- (macOS/Linux). The other end of each pair is opened by an external
-- device_sim.py process pretending to be a PC plugged into that port.
--
-- PROTOCOL: plain text lines, "SRC:DST:MESSAGE\n"
--   SRC / DST are short device ids, e.g. "PC1", "PC2", "SRV"
--   "ALL" as DST means broadcast to every device
-- ============================================================

local PORT_COUNT = 4

-- getCompList is NOT a built-in global — it must be defined here.
-- Returns a list of gdt components named prefix0, prefix1, ... prefixN
local function getCompList(prefix, start, stop)
	local list = {}
	for i = 1, stop - start + 1 do
		list[i] = gdt[prefix .. tostring(i + start - 1)]
	end
	return list
end

local videochip = gdt.VideoChip0
local font = gdt.ROM.System.SpriteSheets["StandardFont"]

-- Assign this VideoChip to EVERY Screen module present, not just
-- Screen0 — the combined drawing buffer only grows to include screens
-- that have actually been wired up here. Auto-detects how many
-- screens exist (stops at the first missing index).
local screenIndex = 0
while gdt["Screen" .. screenIndex] ~= nil do
	gdt["Screen" .. screenIndex].VideoChip = videochip
	screenIndex = screenIndex + 1
end

-- ---- EDIT THESE to match your com0com/socat virtual port numbers ----
local COM_PORTS = { 3, 5, 7, 9 } -- example: the "game side" of each pair
-- -----------------------------------------------------------------

local ports = {}
for i = 1, PORT_COUNT do
	local serial = getCompList("Serial", i - 1, i - 1)[1]
	serial.Port = COM_PORTS[i]
	serial.ReceiveMode = SerialReceiveMode.Lines

	ports[i] = {
		serial = serial,
		led = getCompList("Led", i - 1, i - 1)[1],
		ledTimer = 0,
	}
end

-- MAC/id learning table: device id string -> port index
local addrTable = {}

-- Rolling log of recent switching decisions, newest first
local logLines = {}
local MAX_LOG_LINES = 20

local function pushLog(line)
	table.insert(logLines, 1, line)
	while #logLines > MAX_LOG_LINES do
		table.remove(logLines)
	end
end

local function flashPort(i)
	ports[i].led.State = true
	ports[i].ledTimer = 6
end

-- Parses "SRC:DST:MESSAGE" -> src, dst, message (nil if malformed)
local function parseFrame(line)
	local src, dst, msg = string.match(line, "^(.-):(.-):(.*)$")
	return src, dst, msg
end

-- Core switch logic: called once per received line/frame
local function handleFrame(srcPort, line)
	local src, dst, msg = parseFrame(line)
	if src == nil or dst == nil then
		pushLog("(bad frame on P" .. srcPort .. ")")
		return
	end

	addrTable[src] = srcPort
	flashPort(srcPort)

	if dst == "ALL" then
		for i = 1, PORT_COUNT do
			if i ~= srcPort then
				ports[i].serial:Println(line)
				flashPort(i)
			end
		end
		pushLog(src .. " -> ALL  (flood)")
		return
	end

	local knownPort = addrTable[dst]
	if knownPort ~= nil and knownPort ~= srcPort then
		ports[knownPort].serial:Println(line)
		flashPort(knownPort)
		pushLog(src .. " -> " .. dst .. "  (fwd P" .. knownPort .. ")")
	else
		-- unknown destination: flood to all other ports
		for i = 1, PORT_COUNT do
			if i ~= srcPort then
				ports[i].serial:Println(line)
				flashPort(i)
			end
		end
		pushLog(src .. " -> " .. dst .. "  (flood, unknown)")
	end
end

-- One of these per wired Serial module (see wiring notes above)
function eventChannel1(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleFrame(1, line)
		end
	end
end

function eventChannel2(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleFrame(2, line)
		end
	end
end

function eventChannel3(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleFrame(3, line)
		end
	end
end

function eventChannel4(sender, event)
	if event.Type == "SerialReceiveEvent" then
		for _, line in ipairs(event.Lines) do
			handleFrame(4, line)
		end
	end
end

-- Layout is computed from the VideoChip's actual combined Width/Height
-- (this grows automatically if you wire up more than one Screen), so
-- nothing here assumes a fixed screen size.
local LINE_HEIGHT = 8
local MARGIN = 4

local function drawUI()
	videochip:Clear(color.black)

	local y = MARGIN
	videochip:DrawText(vec2(MARGIN, y), font, "-- SERIAL SWITCH --", color.white, color.black)
	y = y + LINE_HEIGHT * 2

	for i = 1, PORT_COUNT do
		local active = ports[i].serial.IsActive
		local statusColor = active and color.green or color.red
		local statusText = active and "LINK" or "DOWN"

		videochip:DrawText(vec2(MARGIN, y), font, "P" .. i, color.white, color.black)
		videochip:DrawText(vec2(MARGIN + 16, y), font, statusText, statusColor, color.black)
		videochip:DrawText(vec2(MARGIN + 56, y), font, "COM" .. tostring(COM_PORTS[i]), color.grey, color.black)
		y = y + LINE_HEIGHT
	end

	y = y + LINE_HEIGHT
	videochip:DrawText(vec2(MARGIN, y), font, "-- TRAFFIC LOG --", color.white, color.black)
	y = y + LINE_HEIGHT

	-- fill remaining vertical space with as many log lines as fit
	local maxLines = math.floor((videochip.Height - y) / LINE_HEIGHT)
	for i = 1, math.min(maxLines, #logLines) do
		videochip:DrawText(vec2(MARGIN, y), font, logLines[i], color.cyan, color.black)
		y = y + LINE_HEIGHT
	end
end

function update()
	for i = 1, PORT_COUNT do
		if ports[i].ledTimer > 0 then
			ports[i].ledTimer -= 1
			if ports[i].ledTimer == 0 then
				ports[i].led.State = false
			end
		end
	end
	drawUI()
end
