import asyncio
import functools
import itertools
import re
from asyncio import Task
from pathlib import Path
from typing import Any, Optional

import orjson as json
from httpx import AsyncClient, ReadTimeout

from .constants import Endpoint, ErrorCode, Headers, Model
from .exceptions import (
    AuthError,
    APIError,
    ImageGenerationError,
    TimeoutError,
    GeminiError,
    UsageLimitExceeded,
    ModelInvalid,
    TemporarilyBlocked,
)
from .types import WebImage, GeneratedImage, Candidate, ModelOutput, Gem, GemJar
from .utils import (
    upload_file,
    parse_file_name,
    rotate_1psidts,
    get_access_token,
    load_browser_cookies,
    rotate_tasks,
    logger,
)


def running(retry: int = 0) -> callable:
    """
    Decorator to check if client is running before making a request.

    Parameters
    ----------
    retry: `int`, optional
        Max number of retries when `gemini_webapi.APIError` is raised.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(client: "GeminiClient", *args, retry=retry, **kwargs):
            try:
                if not client.running:
                    await client.init(
                        timeout=client.timeout,
                        auto_close=client.auto_close,
                        close_delay=client.close_delay,
                        auto_refresh=client.auto_refresh,
                        refresh_interval=client.refresh_interval,
                        verbose=False,
                    )
                    if client.running:
                        return await func(client, *args, **kwargs)

                    # Should not reach here
                    raise APIError(
                        f"Invalid function call: GeminiClient.{func.__name__}. Client initialization failed."
                    )
                else:
                    return await func(client, *args, **kwargs)
            except APIError as e:
                # Image generation takes too long, only retry once
                if isinstance(e, ImageGenerationError):
                    retry = min(1, retry)

                if retry > 0:
                    await asyncio.sleep(1)
                    return await wrapper(client, *args, retry=retry - 1, **kwargs)

                raise

        return wrapper

    return decorator


class GeminiClient:
    """
    Async httpx client interface for gemini.google.com.

    `secure_1psid` must be provided unless the optional dependency `browser-cookie3` is installed and
    you have logged in to google.com in your local browser.

    Parameters
    ----------
    secure_1psid: `str`, optional
        __Secure-1PSID cookie value.
    secure_1psidts: `str`, optional
        __Secure-1PSIDTS cookie value, some google accounts don't require this value, provide only if it's in the cookie list.
    proxy: `str`, optional
        Proxy URL.
    kwargs: `dict`, optional
        Additional arguments which will be passed to the http client.
        Refer to `httpx.AsyncClient` for more information.

    Raises
    ------
    `ValueError`
        If `browser-cookie3` is installed but cookies for google.com are not found in your local browser storage.
    """

    __slots__ = [
        "cookies",
        "proxy",
        "running",
        "client",
        "access_token",
        "timeout",
        "auto_close",
        "close_delay",
        "close_task",
        "auto_refresh",
        "refresh_interval",
        "_gems",
        "kwargs",
    ]

    def __init__(
        self,
        secure_1psid: str | None = None,
        secure_1psidts: str | None = None,
        proxy: str | None = None,
        **kwargs,
    ):
        self.cookies = {}
        self.proxy = proxy
        self.running: bool = False
        self.client: AsyncClient | None = None
        self.access_token: str | None = None
        self.timeout: float = 300
        self.auto_close: bool = False
        self.close_delay: float = 300
        self.close_task: Task | None = None
        self.auto_refresh: bool = True
        self.refresh_interval: float = 540
        self._gems: GemJar | None = None
        self.kwargs = kwargs

        # Validate cookies
        if secure_1psid:
            self.cookies["__Secure-1PSID"] = secure_1psid
            if secure_1psidts:
                self.cookies["__Secure-1PSIDTS"] = secure_1psidts
        else:
            try:
                cookies = load_browser_cookies(domain_name="google.com")
                if not (cookies and cookies.get("__Secure-1PSID")):
                    raise ValueError(
                        "Failed to load cookies from local browser. Please pass cookie values manually."
                    )
            except ImportError:
                pass

    async def init(
        self,
        timeout: float = 30,
        auto_close: bool = False,
        close_delay: float = 300,
        auto_refresh: bool = True,
        refresh_interval: float = 540,
        verbose: bool = True,
    ) -> None:
        """
        Get SNlM0e value as access token. Without this token posting will fail with 400 bad request.

        Parameters
        ----------
        timeout: `float`, optional
            Request timeout of the client in seconds. Used to limit the max waiting time when sending a request.
        auto_close: `bool`, optional
            If `True`, the client will close connections and clear resource usage after a certain period
            of inactivity. Useful for always-on services.
        close_delay: `float`, optional
            Time to wait before auto-closing the client in seconds. Effective only if `auto_close` is `True`.
        auto_refresh: `bool`, optional
            If `True`, will schedule a task to automatically refresh cookies in the background.
        refresh_interval: `float`, optional
            Time interval for background cookie refresh in seconds. Effective only if `auto_refresh` is `True`.
        verbose: `bool`, optional
            If `True`, will print more infomation in logs.
        """

        try:
            access_token, valid_cookies = await get_access_token(
                base_cookies=self.cookies, proxy=self.proxy, verbose=verbose
            )

            self.client = AsyncClient(
                http2=True,
                timeout=timeout,
                proxy=self.proxy,
                follow_redirects=True,
                headers=Headers.GEMINI.value,
                cookies=valid_cookies,
                **self.kwargs,
            )
            self.access_token = access_token
            self.cookies = valid_cookies
            self.running = True

            self.timeout = timeout
            self.auto_close = auto_close
            self.close_delay = close_delay
            if self.auto_close:
                await self.reset_close_task()

            self.auto_refresh = auto_refresh
            self.refresh_interval = refresh_interval
            if task := rotate_tasks.get(self.cookies["__Secure-1PSID"]):
                task.cancel()
            if self.auto_refresh:
                rotate_tasks[self.cookies["__Secure-1PSID"]] = asyncio.create_task(
                    self.start_auto_refresh()
                )

            if verbose:
                logger.success("Gemini client initialized successfully.")
        except Exception:
            await self.close()
            raise

    async def close(self, delay: float = 0) -> None:
        """
        Close the client after a certain period of inactivity, or call manually to close immediately.

        Parameters
        ----------
        delay: `float`, optional
            Time to wait before closing the client in seconds.
        """

        if delay:
            await asyncio.sleep(delay)

        self.running = False

        if self.close_task:
            self.close_task.cancel()
            self.close_task = None

        if self.client:
            await self.client.aclose()

    async def reset_close_task(self) -> None:
        """
        Reset the timer for closing the client when a new request is made.
        """

        if self.close_task:
            self.close_task.cancel()
            self.close_task = None
        self.close_task = asyncio.create_task(self.close(self.close_delay))

    async def start_auto_refresh(self) -> None:
        """
        Start the background task to automatically refresh cookies.
        """

        while True:
            try:
                new_1psidts = await rotate_1psidts(self.cookies, self.proxy)
            except AuthError:
                if task := rotate_tasks.get(self.cookies["__Secure-1PSID"]):
                    task.cancel()
                logger.warning(
                    "Failed to refresh cookies. Background auto refresh task canceled."
                )

            logger.debug(f"Cookies refreshed. New __Secure-1PSIDTS: {new_1psidts}")
            if new_1psidts:
                self.cookies["__Secure-1PSIDTS"] = new_1psidts
            await asyncio.sleep(self.refresh_interval)

    @property
    def gems(self) -> GemJar:
        """
        Returns a `GemJar` object containing cached gems.
        Only available after calling `GeminiClient.fetch_gems()`.

        Returns
        -------
        :class:`GemJar`
            Refer to `gemini_webapi.types.GemJar`.

        Raises
        ------
        `RuntimeError`
            If `GeminiClient.fetch_gems()` has not been called before accessing this property.
        """

        if self._gems is None:
            raise RuntimeError(
                "Gems not fetched yet. Call `GeminiClient.fetch_gems()` method to fetch gems from gemini.google.com."
            )

        return self._gems

    @running(retry=2)
    async def fetch_gems(self, **kwargs) -> GemJar:
        """
        Get a list of available gems from gemini, including system predefined gems and user-created custom gems.

        Note that network request will be sent every time this method is called.
        Once the gems are fetched, they will be cached and accessible via `GeminiClient.gems` property.

        Returns
        -------
        :class:`GemJar`
            Refer to `gemini_webapi.types.GemJar`.
        """

        try:
            response = await self.client.post(
                Endpoint.BATCH_EXEC,
                data={
                    "at": self.access_token,
                    "f.req": json.dumps(
                        [
                            [
                                ["CNgdBe", '[2,["en"],0]', None, "custom"],
                                ["CNgdBe", '[3,["en"],0]', None, "system"],
                            ]
                        ]
                    ).decode(),
                },
                **kwargs,
            )
        except ReadTimeout:
            raise TimeoutError(
                "Fetch gems request timed out, please try again. If the problem persists, "
                "consider setting a higher `timeout` value when initializing GeminiClient."
            )

        if response.status_code != 200:
            raise APIError(
                f"Failed to fetch gems. Request failed with status code {response.status_code}"
            )
        else:
            try:
                response_json = json.loads(response.text.split("\n")[2])

                predefined_gems, custom_gems = [], []

                for part in response_json:
                    if part[-1] == "system":
                        predefined_gems = json.loads(part[2])[2]
                    elif part[-1] == "custom":
                        if custom_gems_container := json.loads(part[2]):
                            custom_gems = custom_gems_container[2]

                if not predefined_gems and not custom_gems:
                    raise Exception
            except Exception:
                await self.close()
                logger.debug(f"Invalid response: {response.text}")
                raise APIError(
                    "Failed to fetch gems. Invalid response data received. Client will try to re-initialize on next request."
                )

        self._gems = GemJar(
            itertools.chain(
                (
                    (
                        gem[0],
                        Gem(
                            id=gem[0],
                            name=gem[1][0],
                            description=gem[1][1],
                            prompt=gem[2] and gem[2][0] or None,
                            predefined=True,
                        ),
                    )
                    for gem in predefined_gems
                ),
                (
                    (
                        gem[0],
                        Gem(
                            id=gem[0],
                            name=gem[1][0],
                            description=gem[1][1],
                            prompt=gem[2] and gem[2][0] or None,
                            predefined=False,
                        ),
                    )
                    for gem in custom_gems
                ),
            )
        )

        return self._gems

    @running(retry=2)
    async def generate_content(
        self,
        prompt: str,
        files: list[str | Path] | None = None,
        model: Model | str = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
        chat: Optional["ChatSession"] = None,
        **kwargs,
    ) -> ModelOutput:
        """
        Generates contents with prompt.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        model: `Model` | `str`, optional
            Specify the model to use for generation.
            Pass either a `gemini_webapi.constants.Model` enum or a model name string.
        gem: `Gem | str`, optional
            Specify a gem to use as system prompt for the chat session.
            Pass either a `gemini_webapi.types.Gem` object or a gem id string.
        chat: `ChatSession`, optional
            Chat data to retrieve conversation history. If None, will automatically generate a new chat id when sending post request.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `httpx.AsyncClient.request` for more information.

        Returns
        -------
        :class:`ModelOutput`
            Output data from gemini.google.com, use `ModelOutput.text` to get the default text reply, `ModelOutput.images` to get a list
            of images in the default reply, `ModelOutput.candidates` to get a list of all answer candidates in the output.

        Raises
        ------
        `AssertionError`
            If prompt is empty.
        `gemini_webapi.TimeoutError`
            If request timed out.
        `gemini_webapi.GeminiError`
            If no reply candidate found in response.
        `gemini_webapi.APIError`
            - If request failed with status code other than 200.
            - If response structure is invalid and failed to parse.
        """

        assert prompt, "Prompt cannot be empty."

        if not isinstance(model, Model):
            model = Model.from_name(model)

        if isinstance(gem, Gem):
            gem = gem.id

        if self.auto_close:
            await self.reset_close_task()

        try:
            response = await self.client.post(
                Endpoint.GENERATE.value,
                headers=model.model_header,
                data={
                    "at": self.access_token,
                    "f.req": json.dumps(
                        [
                            None,
                            json.dumps(
                                [
                                    files
                                    and [
                                        prompt,
                                        0,
                                        None,
                                        [
                                            [
                                                [await upload_file(file, self.proxy)],
                                                parse_file_name(file),
                                            ]
                                            for file in files
                                        ],
                                    ]
                                    or [prompt],
                                    None,
                                    chat and chat.metadata,
                                ]
                                + (gem and [None] * 16 + [gem] or [])
                            ).decode(),
                        ]
                    ).decode(),
                },
                **kwargs,
            )
        except ReadTimeout:
            raise TimeoutError(
                "Generate content request timed out, please try again. If the problem persists, "
                "consider setting a higher `timeout` value when initializing GeminiClient."
            )

        if response.status_code != 200:
            await self.close()
            raise APIError(
                f"Failed to generate contents. Request failed with status code {response.status_code}"
            )
        else:
            try:
                response_json = json.loads(response.text.split("\n")[2])

                body = None
                body_index = 0
                for part_index, part in enumerate(response_json):
                    try:
                        main_part = json.loads(part[2])
                        if main_part[4]:
                            body_index, body = part_index, main_part
                            break
                    except (IndexError, TypeError, ValueError):
                        continue

                if not body:
                    raise Exception
            except Exception:
                await self.close()

                try:
                    match ErrorCode(response_json[0][5][2][0][1][0]):
                        case ErrorCode.USAGE_LIMIT_EXCEEDED:
                            raise UsageLimitExceeded(
                                f"Failed to generate contents. Usage limit of {model.model_name} model has exceeded. Please try switching to another model."
                            )
                        case ErrorCode.MODEL_HEADER_INVALID:
                            raise ModelInvalid(
                                "Failed to generate contents. The specified model is not available. Please update gemini_webapi to the latest version. "
                                "If the error persists and is caused by the package, please report it on GitHub."
                            )
                        case ErrorCode.IP_TEMPORARILY_BLOCKED:
                            raise TemporarilyBlocked(
                                "Failed to generate contents. Your IP address is temporarily blocked by Google. Please try using a proxy or waiting for a while."
                            )
                        case _:
                            raise Exception
                except GeminiError:
                    raise
                except Exception:
                    logger.debug(f"Invalid response: {response.text}")
                    raise APIError(
                        "Failed to generate contents. Invalid response data received. Client will try to re-initialize on next request."
                    )

            try:
                candidates = []
                for candidate_index, candidate in enumerate(body[4]):
                    text = candidate[1][0]
                    if re.match(
                        r"^http://googleusercontent\.com/card_content/\d+", text
                    ):
                        text = candidate[22] and candidate[22][0] or text

                    try:
                        thoughts = candidate[37][0][0]
                    except (TypeError, IndexError):
                        thoughts = None

                    web_images = (
                        candidate[12]
                        and candidate[12][1]
                        and [
                            WebImage(
                                url=web_image[0][0][0],
                                title=web_image[7][0],
                                alt=web_image[0][4],
                                proxy=self.proxy,
                            )
                            for web_image in candidate[12][1]
                        ]
                        or []
                    )

                    generated_images = []
                    if candidate[12] and candidate[12][7] and candidate[12][7][0]:
                        img_body = None
                        for img_part_index, part in enumerate(response_json):
                            if img_part_index < body_index:
                                continue

                            try:
                                img_part = json.loads(part[2])
                                if img_part[4][candidate_index][12][7][0]:
                                    img_body = img_part
                                    break
                            except (IndexError, TypeError, ValueError):
                                continue

                        if not img_body:
                            raise ImageGenerationError(
                                "Failed to parse generated images. Please update gemini_webapi to the latest version. "
                                "If the error persists and is caused by the package, please report it on GitHub."
                            )

                        img_candidate = img_body[4][candidate_index]

                        text = re.sub(
                            r"http://googleusercontent\.com/image_generation_content/\d+",
                            "",
                            img_candidate[1][0],
                        ).rstrip()

                        generated_images = [
                            GeneratedImage(
                                url=generated_image[0][3][3],
                                title=f"[Generated Image {generated_image[3][6]}]",
                                alt=len(generated_image[3][5]) > image_index
                                and generated_image[3][5][image_index]
                                or generated_image[3][5][0],
                                proxy=self.proxy,
                                cookies=self.cookies,
                            )
                            for image_index, generated_image in enumerate(
                                img_candidate[12][7][0]
                            )
                        ]

                    candidates.append(
                        Candidate(
                            rcid=candidate[0],
                            text=text,
                            thoughts=thoughts,
                            web_images=web_images,
                            generated_images=generated_images,
                        )
                    )
                if not candidates:
                    raise GeminiError(
                        "Failed to generate contents. No output data found in response."
                    )

                output = ModelOutput(metadata=body[1], candidates=candidates)
            except (TypeError, IndexError):
                logger.debug(f"Invalid response: {response.text}")
                raise APIError(
                    "Failed to parse response body. Data structure is invalid."
                )

            if isinstance(chat, ChatSession):
                chat.last_output = output

            return output

    def start_chat(self, **kwargs) -> "ChatSession":
        """
        Returns a `ChatSession` object attached to this client.

        Parameters
        ----------
        kwargs: `dict`, optional
            Additional arguments which will be passed to the chat session.
            Refer to `gemini_webapi.ChatSession` for more information.

        Returns
        -------
        :class:`ChatSession`
            Empty chat session object for retrieving conversation history.
        """

        return ChatSession(geminiclient=self, **kwargs)


class ChatSession:
    """
    Chat data to retrieve conversation history. Only if all 3 ids are provided will the conversation history be retrieved.

    Parameters
    ----------
    geminiclient: `GeminiClient`
        Async httpx client interface for gemini.google.com.
    metadata: `list[str]`, optional
        List of chat metadata `[cid, rid, rcid]`, can be shorter than 3 elements, like `[cid, rid]` or `[cid]` only.
    cid: `str`, optional
        Chat id, if provided together with metadata, will override the first value in it.
    rid: `str`, optional
        Reply id, if provided together with metadata, will override the second value in it.
    rcid: `str`, optional
        Reply candidate id, if provided together with metadata, will override the third value in it.
    model: `Model` | `str`, optional
        Specify the model to use for generation.
        Pass either a `gemini_webapi.constants.Model` enum or a model name string.
    gem: `Gem | str`, optional
        Specify a gem to use as system prompt for the chat session.
        Pass either a `gemini_webapi.types.Gem` object or a gem id string.
    """

    __slots__ = [
        "__metadata",
        "geminiclient",
        "last_output",
        "model",
        "gem",
    ]

    def __init__(
        self,
        geminiclient: GeminiClient,
        metadata: list[str | None] | None = None,
        cid: str | None = None,  # chat id
        rid: str | None = None,  # reply id
        rcid: str | None = None,  # reply candidate id
        model: Model | str = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
    ):
        self.__metadata: list[str | None] = [None, None, None]
        self.geminiclient: GeminiClient = geminiclient
        self.last_output: ModelOutput | None = None
        self.model: Model | str = model
        self.gem: Gem | str | None = gem

        if metadata:
            self.metadata = metadata
        if cid:
            self.cid = cid
        if rid:
            self.rid = rid
        if rcid:
            self.rcid = rcid

    def __str__(self):
        return f"ChatSession(cid='{self.cid}', rid='{self.rid}', rcid='{self.rcid}')"

    __repr__ = __str__

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        # update conversation history when last output is updated
        if name == "last_output" and isinstance(value, ModelOutput):
            self.metadata = value.metadata
            self.rcid = value.rcid

    async def send_message(
        self,
        prompt: str,
        files: list[str | Path] | None = None,
        **kwargs,
    ) -> ModelOutput:
        """
        Generates contents with prompt.
        Use as a shortcut for `GeminiClient.generate_content(prompt, image, self)`.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `httpx.AsyncClient.request` for more information.

        Returns
        -------
        :class:`ModelOutput`
            Output data from gemini.google.com, use `ModelOutput.text` to get the default text reply, `ModelOutput.images` to get a list
            of images in the default reply, `ModelOutput.candidates` to get a list of all answer candidates in the output.

        Raises
        ------
        `AssertionError`
            If prompt is empty.
        `gemini_webapi.TimeoutError`
            If request timed out.
        `gemini_webapi.GeminiError`
            If no reply candidate found in response.
        `gemini_webapi.APIError`
            - If request failed with status code other than 200.
            - If response structure is invalid and failed to parse.
        """

        return await self.geminiclient.generate_content(
            prompt=prompt,
            files=files,
            model=self.model,
            gem=self.gem,
            chat=self,
            **kwargs,
        )

    def choose_candidate(self, index: int) -> ModelOutput:
        """
        Choose a candidate from the last `ModelOutput` to control the ongoing conversation flow.

        Parameters
        ----------
        index: `int`
            Index of the candidate to choose, starting from 0.

        Returns
        -------
        :class:`ModelOutput`
            Output data of the chosen candidate.

        Raises
        ------
        `ValueError`
            If no previous output data found in this chat session, or if index exceeds the number of candidates in last model output.
        """

        if not self.last_output:
            raise ValueError("No previous output data found in this chat session.")

        if index >= len(self.last_output.candidates):
            raise ValueError(
                f"Index {index} exceeds the number of candidates in last model output."
            )

        self.last_output.chosen = index
        self.rcid = self.last_output.rcid
        return self.last_output

    @property
    def metadata(self):
        return self.__metadata

    @metadata.setter
    def metadata(self, value: list[str]):
        if len(value) > 3:
            raise ValueError("metadata cannot exceed 3 elements")
        self.__metadata[: len(value)] = value

    @property
    def cid(self):
        return self.__metadata[0]

    @cid.setter
    def cid(self, value: str):
        self.__metadata[0] = value

    @property
    def rid(self):
        return self.__metadata[1]

    @rid.setter
    def rid(self, value: str):
        self.__metadata[1] = value

    @property
    def rcid(self):
        return self.__metadata[2]

    @rcid.setter
    def rcid(self, value: str):
        self.__metadata[2] = value
