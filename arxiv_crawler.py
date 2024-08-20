from bs4 import BeautifulSoup, NavigableString, Tag
import re
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
import asyncio
import aiohttp
from datetime import datetime
from typing_extensions import Literal
from paper import PaperDatabase, Paper


class ArxivScraper(object):
    def __init__(
        self,
        date_from,
        date_until,
        category_blacklist=[],
        category_whitelist=["cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.IR", "cs.MA"],
        optional_keywords=["LLM", "LLMs", "language model", "language models", "multimodal", "finetuning", "GPT"],
        trans_to="zh-CN",
        filt_date_type: Literal["submitted_date_first", "announced_date_first"] = "submitted_date_first",
        proxy=None
    ):
        """
        一个抓取指定日期范围内的arxiv文章的类,
        搜索基于https://arxiv.org/search/advanced,
        一个文件被爬取到的条件是：首次提交时间在`date_from`和`date_until`之间，并且包含至少一个关键词。
        一个文章被详细展示（不被过滤）的条件是：至少有一个领域在白名单中，并且没有任何一个领域在黑名单中。
        翻译基于google-translate

        Args:
            date_from (str): 开始日期(含当天)
            date_until (str): 结束日期(不含当天)
            category_blacklist (list, optional): 黑名单. Defaults to [].
            category_whitelist (list, optional): 白名单. Defaults to ["cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.IR", "cs.MA"].
            optional_keywords (list, optional): 关键词, 各词之间关系为OR, 在标题/摘要中至少要出现一个关键词才会被爬取.
                Defaults to [ "LLM", "LLMs", "language model", "language models", "multimodal", "finetuning", "GPT"]
            trans_to: 翻译的目标语言, 若设为可转换为False的值则不会翻译
            filt_date_type (Literal["submitted_date_first", "announced_date_first"], optional): 日期筛选方式. Defaults to "submitted_date_first".
                注意，如果指定为"announced_date_first"，则会按照公布日期筛选，这样的筛选是最全的，但日期**只有年月部分**有效，日部分会被忽略。
            proxy (str | None, optional): 用于翻译和爬取arxiv时要使用的代理, 通常是http://127.0.0.1:7890. Defaults to None
        """
        self.date_from = date_from  # url
        self.date_until = date_until  # url
        self.category_blacklist = category_blacklist  # used as metadata
        self.category_whitelist = category_whitelist  # used as metadata
        self.optional_keywords = [kw.replace(" ", "+") for kw in optional_keywords]  # url转义
        self.filt_date_by = filt_date_type
        self.trans_to = trans_to  # translate
        self.proxy = proxy
        
        self.order = "-announced_date_first"  # url(结果默认按首次公布日期的降序排列，这样最新公布的会在前面)
        self.total = None  # fetch_all
        self.step = 50  # url, fetch_all
        self.papers: list[Paper] = []  # fetch_all

        self.paper_db = PaperDatabase(date_from, date_until, category_blacklist, category_whitelist)
        self.console = Console()

    @property
    def meta_data(self):
        """
        返回搜索的元数据
        """
        return dict(repo_url="https://github.com/huiyeruzhou/arxiv_crawler", **self.__dict__)

    def get_url(self, start):
        """
        获取用于搜索的url

        Args:
            start (int): 返回结果的起始序号, 每个页面只会包含序号为[start, start+50)的文章
            filter_date_by (str, optional): 日期筛选方式. Defaults to "submitted_date_first".
        """
        # https://arxiv.org/search/advanced?terms-0-operator=AND&terms-0-term=LLM&terms-0-field=all&terms-1-operator=OR&terms-1-term=language+model&terms-1-field=all&terms-2-operator=OR&terms-2-term=multimodal&terms-2-field=all&terms-3-operator=OR&terms-3-term=finetuning&terms-3-field=all&terms-4-operator=AND&terms-4-term=GPT&terms-4-field=all&classification-computer_science=y&classification-physics_archives=all&classification-include_cross_list=include&date-year=&date-filter_by=date_range&date-from_date=2024-08-08&date-to_date=2024-08-15&date-date_type=submitted_date_first&abstracts=show&size=50&order=submitted_date
        kwargs = "".join(f"&terms-{i}-operator=OR&terms-{i}-term={kw}&terms-{i}-field=all" for i, kw in enumerate(self.optional_keywords))
        return (
            f"https://arxiv.org/search/advanced?advanced={kwargs}"
            + f"&classification-computer_science=y&classification-physics_archives=all&classification-include_cross_list=include&date-year=&date-filter_by=date_range&date-from_date={self.date_from}&date-to_date={self.date_until}&date-date_type={self.filt_date_by}&abstracts=show&size={self.step}&order={self.order}&start={start}"
        )

    async def fetch_all(self):
        """
        (aio)获取所有文章
        """
        # 获取前50篇文章并记录总数
        self.console.log(f"[bold green]Fetching the first {self.step} papers...")
        self.console.print(f"[grey] {self.get_url(0)}")
        content = await self.request(0)
        self.parse_search_html(content)

        # 获取剩余的内容
        with Progress(
            SpinnerColumn(),
            *Progress.get_default_columns(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        ) as p:  # rich进度条
            task = p.add_task(
                description=f"[bold green]Fetching {self.total} results",
                total=self.total,
            )
            p.update(task, advance=self.step)

            async def wrapper(start):  # wrapper用于显示进度
                # 异步请求网页，并解析其中的内容
                content = await self.request(start)
                self.parse_search_html(content)
                p.update(task, advance=self.step)

            # 创建异步任务
            fetch_tasks = []
            for start in range(self.step, self.total, self.step):
                fetch_tasks.append(wrapper(start))
            await asyncio.gather(*fetch_tasks)

        self.console.log(f"[bold green]Fetching completed. ")
        if self.trans_to:
            await self.translate()
        self.paper_db.add_papers(self.papers)

    def fetch_update(self):
        """
        更新文章, 这会从最新公布的文章开始更新, 直到遇到已经爬取过的文章为止。
        为了效率，建议在运行fetch_all后再运行fetch_update
        """
        self.console.log(f"[bold green]Updating the first {self.step} papers...")
        self.console.print(f"[grey] {self.get_url(0)}")

        content = asyncio.run(self.request(0))
        self.parse_search_html(content)
        continue_update = self.update_and_clear_papers()
        for start in range(self.step, self.total, self.step):
            if not continue_update:
                break

            content = asyncio.run(self.request(start))
            self.parse_search_html(content)
            continue_update = self.update_and_clear_papers()

            self.console.log(f"[bold green]Updating the {start}-{start+50} papers...")
        self.console.log("[bold green]Updating completed.")
        return

    def update_and_clear_papers(self):
        if self.trans_to:
            asyncio.run(self.translate())
        continue_update = self.paper_db.update_papers(self.papers)
        self.papers = []
        return continue_update

    async def request(self, start):
        """
        异步请求网页，重试至多3次
        """
        error = 0
        url = self.get_url(start)
        while error <= 3:
            try:
                async with aiohttp.ClientSession(trust_env=True, read_timeout=10) as session:
                    async with session.get(url, proxy=self.proxy) as response:
                        response.raise_for_status()
                        content = await response.text()
                        return content
            except Exception as e:
                error += 1
                self.console.log(f"[bold red]Request {start} cause error: ")
                self.console.print_exception()
                self.console.log(f"[bold red]Retrying {start}... {error}/3")

    def parse_search_html(self, content):
        """
        解析搜索结果页面, 并将结果保存到self.paper_result中
        初次调用时, 会解析self.total

        Args:
            content (str): 网页内容
        """

        """下面是一个搜索结果的例子
        <li class="arxiv-result">
            <div class="is-marginless">
                <p class="list-title is-inline-block">
                    <a href="https://arxiv.org/abs/physics/9403001">arXiv:physics/9403001</a>
                    <span>&nbsp;[<a href="https://arxiv.org/pdf/physics/9403001">pdf</a>, <a
                            href="https://arxiv.org/ps/physics/9403001">ps</a>, <a
                            href="https://arxiv.org/format/physics/9403001">other</a>]&nbsp;</span>
                </p>
                <div class="tags is-inline-block">
                    <span class="tag is-small is-link tooltip is-tooltip-top" data-tooltip="Popular Physics">
                        physics.pop-ph</span>
                    <span class="tag is-small is-grey tooltip is-tooltip-top"
                        data-tooltip="High Energy Physics - Theory">hep-th</span>
                </div>
                <div class="is-inline-block" style="margin-left: 0.5rem">
                    <div class="tags has-addons">
                        <span class="tag is-dark is-size-7">doi</span>
                        <span class="tag is-light is-size-7">
                            <a class="" href="https://doi.org/10.1063/1.2814991">10.1063/1.2814991 <i
                                    class="fa fa-external-link" aria-hidden="true"></i></a>
                        </span>
                    </div>
                </div> 
            </div>
            <p class="title is-5 mathjax">
                Desperately Seeking Superstrings
            </p>
            <p class="authors">
                <span class="has-text-black-bis has-text-weight-semibold">Authors:</span>
                    <a href="/search/?searchtype=author&amp;query=Ginsparg%2C+P">Paul Ginsparg</a>, <a href="/search/?searchtype=author&amp;query=Glashow%2C+S">Sheldon Glashow</a> 
            </p> 
            <p class="abstract mathjax">
                <span class="has-text-black-bis has-text-weight-semibold">Abstract</span>: 
                
                <span class="abstract-short has-text-grey-dark mathjax" id="physics/9403001v1-abstract-short"
                    style="display: inline;"> We provide a detailed analysis of the problems and prospects of superstring theory c.
                1986, anticipating much of the progress of the decades to follow. </span>

                <span class="abstract-full has-text-grey-dark mathjax" id="physics/9403001v1-abstract-full"
                    style="display: none;"> We provide a detailed analysis of the problems and prospects of
                superstring theory c. 1986, anticipating much of the progress of the decades to follow. 
                <a class="is-size-7" style="white-space: nowrap;"
                        onclick="document.getElementById('physics/9403001v1-abstract-full').style.display = 'none'; document.getElementById('physics/9403001v1-abstract-short').style.display = 'inline';">△ Less</a>
                </span>
            </p> 
            <p class="is-size-7"><span class="has-text-black-bis has-text-weight-semibold">Submitted</span>
                25 April, 1986; <span class="has-text-black-bis has-text-weight-semibold">originally
                announced</span> March 1994. </p> 
            <p class="comments is-size-7">
                <span class="has-text-black-bis has-text-weight-semibold">Comments:</span>
                <span class="has-text-grey-dark mathjax">originally appeared as a Reference Frame in Physics
                    Today, May 1986</span>
            </p> 
            <p class="comments is-size-7">
                <span class="has-text-black-bis has-text-weight-semibold">Journal ref:</span> Phys.Today
                86N5 (1986) 7-9 </p> 
        </li>
        """

        soup = BeautifulSoup(content, "html.parser")
        if not self.total:
            total = soup.select("#main-container > div.level.is-marginless > div.level-left > h1")[0].text
            # "Showing 1–50 of 2,542,002 results" or "Sorry, your query returned no results"
            if "Sorry" in total:
                self.total = 0
                return
            total = int(total[total.find("of") + 3 : total.find("results")].replace(",", ""))
            self.total = total

        results = soup.find_all("li", {"class": "arxiv-result"})
        for result in results:

            url_tag = result.find("a")
            url = url_tag["href"] if url_tag else "No link"

            title_tag = result.find("p", class_="title")
            title = self.parse_search_text(title_tag) if title_tag else "No title"

            date_tag = result.find("p", class_="is-size-7")
            date = date_tag.get_text(strip=True) if date_tag else "No date"
            if "v1" in date:
                # Submitted9 August, 2024; v1submitted 8 August, 2024; originally announced August 2024.
                # 注意空格会被吞掉，这里我们要找最早的提交日期
                v1 = date.find("v1submitted")
                date = date[v1 + 12 : date.find(";", v1)]
            else:
                # Submitted8 August, 2024; originally announced August 2024.
                # 注意空格会被吞掉
                submit_date = date.find("Submitted")
                date = date[submit_date + 9 : date.find(";", submit_date)]

            category_tag = result.find_all("span", class_="tag")
            categories = [category.get_text(strip=True) for category in category_tag if "tooltip" in category.get("class")]

            authors_tag = result.find("p", class_="authors")
            authors = authors_tag.get_text(strip=True)[len("Authors:") :] if authors_tag else "No authors"

            summary_tag = result.find("span", class_="abstract-full")
            abstract = self.parse_search_text(summary_tag) if summary_tag else "No summary"

            self.papers.append(
                Paper(
                    url=url,
                    title=title,
                    first_submitted_date=datetime.strptime(date, "%d %B, %Y"),
                    categories=categories,
                    authors=authors,
                    abstract=abstract,
                )
            )

    def parse_search_text(self, tag):
        string = ""
        for child in tag.children:
            if isinstance(child, NavigableString):
                string += re.sub(r"\s+", " ", child)
            elif isinstance(child, Tag):
                if child.name == "span" and "search-hit" in child.get("class"):
                    string += re.sub(r"\s+", " ", child.get_text(strip=False))
                elif child.name == "a" and ".style.display" in child.get("onclick"):
                    pass
                else:
                    import pdb

                    pdb.set_trace()
        return string

    async def translate(self):
        if not self.trans_to:
            raise ValueError("No target language specified.")
        self.console.log("[bold green]Translating...")
        with Progress(
            SpinnerColumn(),
            *Progress.get_default_columns(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        ) as p:
            total = len(self.papers)
            task = p.add_task(
                description=f"[bold green]Translating {total} papers",
                total=total,
            )

            async def worker(paper):
                await paper.translate(langto=self.trans_to)
                p.update(task, advance=1)

            await asyncio.gather(*[worker(paper) for paper in self.papers])

    def to_markdown(self, output_dir="./output_llms", filename_format="%Y-%m-%d", meta=False):
        self.paper_db.to_markdown(output_dir, filename_format, self.meta_data if meta else None)

    def to_csv(self, output_dir="./output_llms", filename_format="%Y-%m-%d", csv_config={}):
        self.paper_db.to_csv(output_dir, filename_format, csv_config)


if __name__ == "__main__":
    from datetime import date, timedelta

    today = date.today()
    recent = today - timedelta(days=1)

    date_from = recent.strftime("%Y-%m-%d")
    data_until = today.strftime("%Y-%m-%d")

    scraper = ArxivScraper(
        date_from=date_from,
        date_until=data_until,
    )
    asyncio.run(scraper.fetch_all())
    scraper.to_markdown(meta=True)
