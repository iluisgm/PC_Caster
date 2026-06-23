' PC Caster — minimal Roku channel that plays an HLS/MP4 URL handed
' to it over ECP, either as launch deep-link params or via /input while running.

sub Main(args as Dynamic)
    screen = CreateObject("roSGScreen")
    m.port = CreateObject("roMessagePort")
    screen.setMessagePort(m.port)

    scene = screen.CreateScene("MainScene")
    screen.show()

    ' Deep-link params from /launch/dev?contentId=...&mediaType=...
    if args <> invalid then
        scene.setField("launchArgs", args)
    end if

    ' Receive new URLs via /input while the channel is already running.
    input = CreateObject("roInput")
    input.setMessagePort(m.port)

    while true
        msg = wait(0, m.port)
        mt = type(msg)
        if mt = "roSGScreenEvent" then
            if msg.isScreenClosed() then return
        else if mt = "roInputEvent" then
            if msg.isInput() then
                scene.setField("ecpInput", msg.getInfo())
            end if
        end if
    end while
end sub
