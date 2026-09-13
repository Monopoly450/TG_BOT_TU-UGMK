"""Browser integration checks with mocked Telegram/API; no production writes.

Run inside the project image: python tests/miniapp_browser.py
Screenshots are written to /tmp/miniapp-qa.
"""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PIL import Image
from playwright.async_api import async_playwright


async def main():
    html = Path("templates/webapp.html").read_text()
    marked_path = os.environ.get('MARKED_TEST_SCRIPT')
    marked_script = Path(marked_path).read_text() if marked_path else None
    output = Path("/tmp/miniapp-qa")
    output.mkdir(exist_ok=True)
    models = [
        {"id": "google/gemma-4-26b-a4b-it:free", "name": "Gemma 4 26B", "is_free": True, "supports_images": True, "input_price": 0, "output_price": 0, "description": "Универсальная помощь в учёбе"},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron 3 Super", "is_free": True, "supports_images": False, "description": "Программирование и рассуждения"},
        {"id": "google/gemma-4-31b-it:free", "name": "Gemma 4 31B", "is_free": True, "supports_images": True, "description": "Бесплатная модель"},
    ]
    status = {"ai_model": models[0]["id"], "group_name": "Ит-25107", "can_chat": True,
              "is_starosta": False, "is_admin": False, "morning_time": "08:17", "evening_time": "Отключено"}
    schedule = {"Понедельник": [{"time": "09:00–10:30", "subject": "Математический анализ", "type": "Лекция", "room": "Толк 1", "link": "https://tu-ugmk.ktalk.ru/jiydkhlxmj94", "teacher": "Иванова А. В."},
                               {"time": "10:40–12:10", "subject": "Основы программирования", "type": "Практика", "room": "208", "teacher": "Петров И. С."}], "Вторник": []}
    calls = []
    fail_next = False
    race_mode = False
    pending_count = 0
    writes = []
    async def route_handler(route):
        nonlocal fail_next, pending_count
        path = urlparse(route.request.url).path
        if marked_script and 'cdn.jsdelivr.net/npm/marked/' in route.request.url:
            await route.fulfill(content_type='text/javascript', body=marked_script)
            return
        if route.request.url.startswith("https://telegram.org/"):
            await route.fulfill(content_type="text/javascript", body="window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:1,first_name:'Владислав'}},expand(){},setHeaderColor(){},setBackgroundColor(){}}};")
            return
        if not route.request.url.startswith("https://miniapp.test"):
            await route.abort()
            return
        payload = {}
        if path == "/webapp":
            await route.fulfill(content_type="text/html", body=html)
            return
        if path == "/api/verify": payload = {"status": "ok", "user_status": status, "initial_schedule": schedule}
        elif path == "/api/user_status": payload = status
        elif path == "/api/models": payload = {"models": models}
        elif path == "/api/groups": payload = {"groups": ["Ит-25107", "Ит-26107"]}
        elif path == "/api/schedule":
            group = parse_qs(urlparse(route.request.url).query).get('target_name',['Ит-25107'])[0]
            if race_mode:
                await asyncio.sleep(.3 if group == 'Ит-25107' else .02)
                pending_count += 1
                payload = {'schedule': {'_pending': True}} if pending_count == 1 else {'group_name': group, 'schedule': {'_group':group,'Понедельник':[{'time':'09:00','subject':'Пара '+group}]}}
            else: payload = {"schedule": schedule, 'group_name':group}
        elif path == "/api/ai_history": payload = {"history": []}
        elif path == "/api/request_history": payload = {"requests": []}
        elif path == "/api/ecosystem": payload = {"events": [], "channels": [], "rooms": []}
        elif path.startswith('/api/starosta/') or path == '/api/set_group':
            writes.append((path, route.request.post_data_json))
            payload = {'status':'ok','total':2}
        elif path == "/api/set_model":
            status["ai_model"] = route.request.post_data_json["model"]
            payload = {"status": "ok", "model": status["ai_model"]}
        elif path == "/api/ai_chat":
            calls.append(route.request.post_data_json)
            await asyncio.sleep(.5)
            if fail_next:
                fail_next = False
                await route.fulfill(status=429, json={"detail": "Модель временно занята"})
                return
            payload = {"response": "Рассмотрим задание по шагам.\n\n1. Определим исходные данные.\n2. Подставим значения в формулу.\n\nОтвет: 42."}
        await route.fulfill(json=payload)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        page = await browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        await page.route("**/*", route_handler)
        await page.goto("https://miniapp.test/webapp")
        await page.wait_for_selector("#init-screen", state="hidden")
        await page.wait_for_function("allChatModels.length === 3 && !historyLoading")
        for selector in ['body', '.nav-btn', '#chat-input-field', '.schedule-online button']:
            assert await page.locator(selector).first.evaluate("el => getComputedStyle(el).webkitTapHighlightColor") == 'rgba(0, 0, 0, 0)'
        assert await page.locator('#morning-time-select').input_value() == '08:17'
        assert await page.locator('#evening-time-select').input_value() == 'Отключено'
        assert await page.locator('.week-btn').count() == 2
        assert await page.locator('.online-join').first.get_attribute('href') == 'https://tu-ugmk.ktalk.ru/jiydkhlxmj94'
        await page.locator('.schedule-online button').click()
        assert await page.locator('#tab-ecosystem').evaluate("el => el.classList.contains('active')")
        await page.locator('nav button[onclick*=schedule]').click()
        await page.wait_for_selector(".online-join")
        assert await page.evaluate("() => { loadSchedule(0); return !!document.querySelector('#schedule-container .online-join'); }")
        await page.wait_for_function("!document.getElementById('schedule-container').hasAttribute('aria-busy')")
        await page.screenshot(path=str(output / "schedule-mobile.png"))
        # Week transitions must animate without letting a slower old request win.
        async def week_route(route):
            offset = parse_qs(urlparse(route.request.url).query)['week_offset'][0]
            await asyncio.sleep(.18 if offset == '1' else .03)
            await route.fulfill(json={'schedule': {'Понедельник': [{'time':'09:00', 'subject':'Неделя ' + offset}]}})
        await page.route('**/api/schedule?*', week_route)
        await page.emulate_media(reduced_motion='no-preference')
        await page.locator('.week-btn').nth(1).click()
        assert await page.locator('.week-selector').get_attribute('data-week') == '1'
        await page.wait_for_function("scheduleAnimation?.playState === 'running'")
        assert 'Неделя 1' in await page.locator('#schedule-container').inner_text()
        await page.evaluate('Promise.all([loadSchedule(0), loadSchedule(1), loadSchedule(0)])')
        await page.wait_for_timeout(400)
        assert 'Неделя 0' in await page.locator('#schedule-container').inner_text()
        assert 'Неделя 1' not in await page.locator('#schedule-container').inner_text()
        assert await page.locator('.week-btn').first.get_attribute('aria-pressed') == 'true'
        await page.emulate_media(reduced_motion='reduce')
        await page.evaluate('loadSchedule(1)')
        assert await page.evaluate('scheduleAnimation === null')
        await page.unroute('**/api/schedule?*', week_route)
        await page.evaluate('loadSchedule(0)')
        # Content scrolls behind both rounded glass panels, with no clipped strip.
        layout = await page.evaluate('''() => {
            const tab = document.getElementById('tab-schedule').getBoundingClientRect();
            const header = document.querySelector('header');
            const nav = document.querySelector('nav');
            return {top: tab.top, bottom: tab.bottom, headerBottom: header.getBoundingClientRect().bottom,
                navTop: nav.getBoundingClientRect().top, headerBlur: getComputedStyle(header).backdropFilter,
                navBlur: getComputedStyle(nav).backdropFilter};
        }''')
        assert layout['top'] == 0 and layout['bottom'] == 844, layout
        assert layout['top'] < layout['headerBottom'] < layout['navTop'] < layout['bottom'], layout
        assert layout['headerBlur'] != 'none' and layout['navBlur'] != 'none'
        await page.locator('#tab-schedule').evaluate('el => el.scrollTop = 140')
        await page.screenshot(path=str(output / 'schedule-scrolled-glass.png'))
        await page.locator('#tab-schedule').evaluate('el => el.scrollTop = 0')
        await page.locator("nav button").nth(1).click()
        await page.screenshot(path=str(output / "chat-mobile.png"))
        await page.locator("#model-picker-button").click()
        await page.get_by_role("button", name="Текст", exact=True).click()
        assert await page.locator(".model-option").count() == 1
        await page.locator(".model-option").click()
        await page.wait_for_function("!modelBusy")
        assert await page.locator("#current-model-name").inner_text() == "Nemotron 3 Super"
        image = io.BytesIO()
        Image.new("RGB", (100, 60), "green").save(image, "PNG")
        await page.locator('#attach-button').click()
        await page.wait_for_selector('#photo-dialog[open]')
        await page.screenshot(path=str(output / 'photo-source-mobile.png'))
        camera_choosers = []
        page.on('filechooser', lambda chooser: camera_choosers.append(chooser))
        await page.get_by_role('button', name='Сфотографировать', exact=True).click()
        await page.wait_for_function("(cameraStream && !document.getElementById('camera-shutter').disabled) || !document.getElementById('camera-retry').hidden")
        assert await page.locator('#camera-retry').is_hidden(), await page.locator('#camera-status').inner_text()
        await page.wait_for_function("cameraStream && !document.getElementById('camera-shutter').disabled")
        await page.evaluate('window.firstCameraTrack = cameraStream.getVideoTracks()[0]')
        assert await page.evaluate('cameraStream.getAudioTracks().length') == 0
        camera_box = await page.locator('#camera-dialog').bounding_box()
        assert camera_box['x'] == 0 and camera_box['y'] == 0
        assert camera_box['width'] == 390 and camera_box['height'] == 844
        assert await page.locator('#camera-dialog').get_by_role('button', name='Выбрать из галереи', exact=True).count() == 0
        await page.evaluate('openCamera()')
        assert await page.evaluate('cameraStream.getVideoTracks()[0] === firstCameraTrack')
        await page.locator('#camera-flip').click()
        await page.wait_for_function("cameraStream && !document.getElementById('camera-shutter').disabled")
        assert await page.evaluate("firstCameraTrack.readyState === 'ended'")
        await page.evaluate('window.capturedTrack = cameraStream.getVideoTracks()[0]')
        await page.screenshot(path=str(output / 'camera-live-mobile.png'))
        await page.locator('#camera-shutter').click()
        assert not camera_choosers, 'Camera action must never invoke the gallery picker'
        await page.wait_for_selector("#model-dialog[open]")
        assert await page.evaluate("capturedTrack.readyState === 'ended' && cameraStream === null")
        assert await page.locator(".model-option").count() == 2
        await page.get_by_role("button", name="Free", exact=True).click()
        assert await page.locator(".model-option").count() == 3
        await page.screenshot(path=str(output / "models-mobile.png"))
        await page.locator(".model-option").last.click()
        await page.wait_for_function("!modelBusy")
        assert (await page.locator('#attachment-thumbnail').get_attribute('src')).startswith('data:image/jpeg')
        # Denied permission stays in the camera screen and can be retried.
        await page.evaluate("() => { window.realGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Denied', 'NotAllowedError'); }; }")
        await page.locator('#attach-button').click()
        await page.get_by_role('button', name='Сфотографировать', exact=True).click()
        await page.wait_for_selector('#camera-retry:visible')
        assert 'Доступ к камере запрещён' in await page.locator('#camera-status').inner_text()
        assert await page.locator('#camera-shutter').is_disabled()
        assert not camera_choosers
        await page.evaluate('() => { navigator.mediaDevices.getUserMedia = window.realGetUserMedia; }')
        await page.locator('#camera-retry').click()
        await page.wait_for_function("cameraStream && !document.getElementById('camera-shutter').disabled")
        await page.evaluate('window.closedTrack = cameraStream.getVideoTracks()[0]')
        await page.get_by_role('button', name='Закрыть камеру', exact=True).click()
        assert await page.evaluate("closedTrack.readyState === 'ended' && cameraStream === null")
        # A late permission result after closing must also stop its stream.
        await page.evaluate("() => { window.cameraRequests = 0; navigator.mediaDevices.getUserMedia = () => { window.cameraRequests++; return new Promise(resolve => { window.resolveCamera = resolve; }); }; }")
        await page.locator('#attach-button').click()
        await page.get_by_role('button', name='Сфотографировать', exact=True).click()
        await page.evaluate('openCamera(); openCamera()')
        assert await page.evaluate('window.cameraRequests') == 1
        await page.get_by_role('button', name='Закрыть камеру', exact=True).click()
        await page.evaluate("async () => { window.lateStream = await realGetUserMedia({video: true}); resolveCamera(lateStream); }")
        await page.wait_for_function("lateStream.getTracks().every(track => track.readyState === 'ended')")
        await page.evaluate('() => { navigator.mediaDevices.getUserMedia = window.realGetUserMedia; }')
        await page.locator('#attach-button').click()
        async with page.expect_file_chooser() as gallery_event:
            await page.get_by_role('button', name='Выбрать из галереи', exact=True).click()
        gallery = await gallery_event.value
        assert await gallery.element.get_attribute('capture') is None
        await gallery.set_files({'name': 'task.png', 'mimeType': 'image/png', 'buffer': image.getvalue()})
        await page.wait_for_function("pendingPhoto && pendingPhoto.startsWith('data:image/png')")
        # Native clipboard text paste, followed by mixed image + text paste.
        await page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        await page.evaluate("navigator.clipboard.writeText('Помоги решить задание')")
        await page.locator('#chat-input-field').fill('')
        await page.locator('#chat-input-field').press('Control+V')
        assert await page.locator('#chat-input-field').input_value() == 'Помоги решить задание'
        await page.evaluate('''async () => {
            const blob = await (await fetch(pendingPhoto)).blob();
            removeAttachment();
            await navigator.clipboard.write([new ClipboardItem({
                'image/png': blob,
                'text/plain': new Blob([' с картинки'], {type: 'text/plain'})
            })]);
        }''')
        await page.locator('#chat-input-field').press('Control+V')
        await page.wait_for_function("pendingPhoto && pendingPhoto.startsWith('data:image/png')")
        assert await page.locator('#chat-input-field').input_value() == 'Помоги решить задание с картинки'
        await page.locator("#chat-send-button").click()
        await page.wait_for_selector('.typing-label')
        assert await page.locator('.typing-label').inner_text() == 'Думаю…'
        assert await page.locator('.typing-label').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') <= 11
        await page.wait_for_function("!chatBusy")
        assert len(calls) == 1 and calls[0]["image"].startswith("data:image/png;base64,")
        assert calls[0]['prompt'] == 'Помоги решить задание с картинки'
        assert await page.locator(".chat-msg.user img").count() == 1
        assert await page.locator(".chat-msg.ai").count() == 1
        ai_box = await page.locator('.chat-msg.ai').bounding_box()
        body_box = await page.locator('.message-content').bounding_box()
        copy_box = await page.locator('.message-copy').bounding_box()
        assert body_box['x'] - ai_box['x'] >= 16
        assert 0 <= copy_box['y'] - body_box['y'] - body_box['height'] <= 10
        if marked_script:
            assert await page.locator('.message-content ol li').count() == 2
            await page.evaluate('''() => appendMessage('ai', '### Пример кода\\n\\n```python\\nif x < 5 and y > 2:\\n    print("a & b")\\n```\\n\\n<img src=x onerror="window.unsafeMarkdown=true">[Ссылка](javascript:alert(1))')''')
            assert 'x < 5 and y > 2' in await page.locator('.message-content pre code').inner_text()
            assert 'a & b' in await page.locator('.message-content pre code').inner_text()
            assert await page.locator('.message-content img, .message-content [onerror], .message-content a[href^="javascript:"]').count() == 0
            assert not await page.evaluate('window.unsafeMarkdown === true')
            await page.evaluate("scrollChatToEnd()")
            await page.screenshot(path=str(output / 'chat-markdown-mobile.png'))
            await page.locator('.chat-msg.ai').last.evaluate('el => el.remove()')
        await page.evaluate("applyAppTheme('light', false);scrollChatToEnd()")
        await page.screenshot(path=str(output / "chat-photo-mobile.png"))
        fail_next = True
        await page.locator("#chat-input-field").fill("Сохрани этот вопрос")
        await page.locator("#chat-send-button").click()
        await page.wait_for_function("!chatBusy")
        assert await page.locator("#chat-input-field").input_value() == "Сохрани этот вопрос"
        assert await page.locator("#chat-error").is_visible()
        await page.locator("[data-chat-mutation]").click()
        await page.wait_for_selector("#chat-welcome")
        for width, height in [(320, 568), (390, 500), (1280, 900)]:
            await page.locator("#chat-input-field").blur()
            await page.wait_for_timeout(30)
            await page.set_viewport_size({"width": width, "height": height})
            for theme in ["light", "dark"]:
                await page.evaluate("theme => applyAppTheme(theme, false)", theme)
                for index, name in enumerate(["schedule", "chat", "ecosystem", "profile"]):
                    await page.locator(f"nav button[onclick*=\"{name}\"]").click()
                    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (width, name)
                    if name == "chat":
                        box = await page.locator("#chat-send-button").bounding_box()
                        assert box and box["y"] >= 0 and box["y"] + box["height"] <= height, (width, height, box)
                        await page.locator("#chat-input-field").focus()
                        assert await page.locator("#chat-input-field").evaluate("el => getComputedStyle(el).outlineStyle") == "none"
                        await page.locator("#chat-messages").evaluate("el => el.scrollTop = el.scrollHeight")
                        last_prompt = await page.locator(".prompt-chip").last.bounding_box()
                        composer = await page.locator(".composer").bounding_box()
                        assert last_prompt["y"] + last_prompt["height"] <= composer["y"], (width, height, last_prompt, composer)
                        await page.screenshot(path=str(output / f"chat-focused-{width}-{theme}.png"))
                    if width == 1280 or (width == 320 and theme == "light"):
                        await page.screenshot(path=str(output / f"{name}-{width}-{theme}.png"))
        # Repeat the keyboard open/close cycle after sending and reopening the tab.
        await page.locator("#chat-input-field").blur()
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(30)
        await page.locator('nav button[onclick*=chat]').click()
        await page.locator("#chat-input-field").fill("Проверка повторного ввода")
        await page.locator("#chat-send-button").click()
        await page.wait_for_function("!chatBusy")
        for _ in range(3):
            await page.locator('#chat-input-field').focus()
            await page.set_viewport_size({"width": 390, "height": 430})
            await page.wait_for_timeout(80)
            box = await page.locator('.composer').bounding_box()
            field = await page.locator('#chat-input-field').bounding_box()
            assert box['height'] <= 50, box
            assert field['height'] == 38, field
            assert box['y'] + box['height'] <= 430, box
            assert await page.locator('nav').is_hidden()
            messages = await page.locator('#chat-messages').bounding_box()
            assert messages['y'] + messages['height'] <= box['y'], (messages, box)
            assert await page.locator('.chat-msg.ai').count() == 1
            await page.screenshot(path=str(output / 'chat-keyboard.png'))
            await page.locator('#chat-input-field').blur()
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_timeout(80)
            assert await page.locator('nav').is_visible()
            await page.locator('nav button[onclick*=schedule]').click()
            await page.locator('nav button[onclick*=chat]').click()
        # Paid catalog selection and draft survive reopening the app.
        models.append({'id':'test/cheap','name':'Недорогая модель','is_free':False,'supports_images':False,'input_price':.1,'output_price':.3,'description':'Текст'})
        await page.evaluate('loadModels(selectedAIModel)')
        await page.locator('#model-picker-button').click()
        await page.get_by_role('button',name='Недорогие',exact=True).click()
        assert await page.locator('.model-option').count() == 1
        assert '$0.1' in await page.locator('.model-option').inner_text()
        await page.screenshot(path=str(output/'models-cheap.png'))
        await page.locator('.model-option').click()
        await page.wait_for_function('!modelBusy')
        await page.locator('#chat-input-field').fill('Мой сохранённый черновик')
        await page.reload()
        await page.wait_for_selector('#init-screen',state='hidden')
        await page.locator('nav button[onclick*=chat]').click()
        assert await page.locator('#chat-input-field').input_value() == 'Мой сохранённый черновик'
        # Starosta tabs, preview and event publication use mocked routes only.
        await page.evaluate("isUserStarosta=true;starostaGroup='Ит-25107';starostaName='Владислав';openStarostaModal()")
        assert await page.locator('#broadcast-preview-button').is_disabled()
        await page.locator('#broadcast-text').fill('Завтра собираемся в аудитории 208.')
        for theme in ['light','dark']:
            await page.evaluate("theme => applyAppTheme(theme, false)", theme)
            await page.screenshot(path=str(output/f'starosta-{theme}.png'))
        await page.locator('#broadcast-preview-button').click()
        assert await page.locator('#broadcast-preview-text').inner_text() == 'Завтра собираемся в аудитории 208.'
        assert not [w for w in writes if w[0] == '/api/starosta/broadcast']
        await page.screenshot(path=str(output/'starosta-preview.png'))
        await page.locator('#broadcast-send-button').click()
        await page.wait_for_selector('#broadcast-preview-dialog',state='hidden')
        assert len([w for w in writes if w[0] == '/api/starosta/broadcast']) == 1
        await page.locator('#st-tab-events').click()
        assert await page.locator('#st-pane-message').is_hidden()
        await page.locator('#event-title').fill('Встреча студсовета')
        await page.locator('#event-description').fill('Обсудим план на семестр')
        await page.locator('#publish-event-button').click()
        await page.wait_for_function('!starostaBusy')
        assert len([w for w in writes if w[0] == '/api/starosta/add_event']) == 1
        await page.screenshot(path=str(output/'starosta-events.png'))
        await page.locator('#st-tab-settings').click()
        assert await page.locator('#st-pane-settings').is_visible()
        await page.keyboard.press('Escape')
        assert await page.locator('#starosta-modal').is_hidden()
        # A late answer for the previous group cannot overwrite the new group.
        race_mode = True
        await page.locator('nav button[onclick*=schedule]').click()
        await page.evaluate("changeGroup('Ит-25107');changeGroup('Ит-26107')")
        await page.wait_for_function("document.getElementById('schedule-container').textContent.includes('Пара Ит-26107')")
        await page.wait_for_timeout(500)
        assert 'Пара Ит-25107' not in await page.locator('#schedule-container').inner_text()
        assert await page.evaluate('currentTargetName') == 'Ит-26107'
        assert [w[1]['group_name'] for w in writes if w[0]=='/api/set_group'][-1] == 'Ит-26107'
        # Exercise real motion as well as the reduced-motion path used above.
        await page.emulate_media(reduced_motion='no-preference')
        await page.evaluate("() => { switchTab('profile', document.querySelector('nav button[onclick*=profile]')); }")
        await page.wait_for_function("document.getElementById('tab-profile').getAnimations().length > 0")
        # Tapping the selected tab again must not restart its entrance animation.
        assert await page.evaluate("() => { const running = tabAnimation; switchTab('profile', document.querySelector('nav button[onclick*=profile]')); return tabAnimation === running; }")
        await page.evaluate("switchTab('chat', document.querySelector('nav button[onclick*=chat]'));switchTab('schedule', document.querySelector('nav button[onclick*=schedule]'));switchTab('chat', document.querySelector('nav button[onclick*=chat]'))")
        await page.wait_for_timeout(450)
        assert await page.locator('.tab-content.active').count() == 1
        assert await page.locator('#tab-chat').evaluate("el => el.classList.contains('active')")
        assert await page.locator('.is-leaving').count() == 0
        assert await page.locator('nav .nav-btn.active').count() == 1
        assert await page.locator('nav').evaluate("el => getComputedStyle(el, '::before').content") != 'none'
        assert await page.locator('#chat-send-button').is_visible()
        await page.evaluate("appendMessage('ai', 'Плавное появление нового ответа.'); appendMessage('ai', 'Сообщение из истории.', false)")
        assert await page.locator('.chat-msg.ai').last.evaluate('el => el.getAnimations().length') == 0
        await page.wait_for_timeout(400)
        assert await page.locator('.message-new').count() == 0
        await page.locator('#attach-button').click()
        await page.wait_for_selector('#photo-dialog[open]')
        await page.get_by_role('button', name='Закрыть выбор фото', exact=True).click()
        await page.wait_for_selector('#photo-dialog', state='hidden')
        await page.locator('#model-picker-button').click()
        await page.wait_for_selector('#model-dialog[open]')
        await page.keyboard.press('Escape')
        await page.wait_for_selector('#model-dialog', state='hidden')
        await page.evaluate("openStarostaModal()")
        await page.wait_for_timeout(300)
        await page.get_by_role('button', name='Закрыть панель старосты', exact=True).click()
        await page.wait_for_selector('#starosta-modal', state='hidden')
        await page.locator('#chat-input-field').fill('Текст после закрытия окон')
        assert await page.locator('#chat-input-field').input_value() == 'Текст после закрытия окон'
        await page.locator('#chat-input-field').blur()
        await page.keyboard.press('Tab')
        assert await page.evaluate("document.activeElement.matches(':focus-visible')")
        await page.evaluate("setAppTheme('dark')")
        await page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        await page.emulate_media(reduced_motion='reduce')
        await page.evaluate("switchTab('profile', document.querySelector('nav button[onclick*=profile]'))")
        assert await page.locator('#tab-profile').evaluate('el => getComputedStyle(el).animationName') == 'none'
        assert not errors, errors
        await browser.close()
    print("Browser checks passed: filters, paid models, photo, saved draft, keyboard, starosta preview/events and schedule switching; no JS errors.")


asyncio.run(main())
