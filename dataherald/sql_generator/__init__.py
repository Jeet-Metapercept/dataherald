"""Base class that all sql generation classes inherit from."""
import datetime
import logging
import os
import re
from abc import ABC, abstractmethod
from queue import Queue
from typing import Any, Dict, List, Tuple

import sqlparse
from langchain.agents.agent import AgentExecutor
from langchain.callbacks.base import BaseCallbackHandler
from langchain.schema import AgentAction, LLMResult
from langchain.schema.messages import BaseMessage
from langchain_community.callbacks import get_openai_callback
from sql_metadata import Parser

from dataherald.config import Component, System
from dataherald.db_scanner.models.types import TableDescription
from dataherald.model.chat_model import ChatModel
from dataherald.repositories.sql_generations import (
    SQLGenerationRepository,
)
from dataherald.sql_database.base import SQLDatabase, SQLInjectionError
from dataherald.sql_database.models.types import DatabaseConnection
from dataherald.sql_generator.create_sql_query_status import create_sql_query_status
from dataherald.types import IntermediateStep, LLMConfig, Prompt, SQLGeneration
from dataherald.utils.strings import contains_line_breaks


class EngineTimeOutORItemLimitError(Exception):
    pass


def replace_unprocessable_characters(text: str) -> str:
    """Replace unprocessable characters with a space."""
    text = text.strip()
    return text.replace(r"\_", "_")


class SQLGenerator(Component, ABC):
    metadata: Any
    llm: ChatModel | None = None

    def __init__(self, system: System, llm_config: LLMConfig):  # noqa: ARG002
        self.system = system
        self.llm_config = llm_config
        self.model = ChatModel(self.system)

    def check_for_time_out_or_tool_limit(self, response: dict) -> dict:
        if (
            response.get("output")
            == "Agent stopped due to iteration limit or time limit."
        ):
            raise EngineTimeOutORItemLimitError(
                "The engine has timed out or reached the tool limit."
            )
        return response

    def remove_markdown(self, query: str) -> str:
        pattern = r"```sql(.*?)```"
        matches = re.findall(pattern, query, re.DOTALL)
        if matches:
            return matches[0].strip()
        return query

    @classmethod
    def get_upper_bound_limit(cls) -> int:
        top_k = os.getenv("UPPER_LIMIT_QUERY_RETURN_ROWS", None)
        if top_k is None or top_k == "":
            top_k = 50
        return top_k if isinstance(top_k, int) else int(top_k)

    def extract_cve_ids(self, query: str) -> list:
        return list(set(re.findall(r"CVE-\d{4}-\d{4,7}", query)))

    def create_sql_query_status(
        self, db: SQLDatabase, query: str, sql_generation: SQLGeneration
    ) -> SQLGeneration:
        return create_sql_query_status(db, query, sql_generation)

    def format_sql_query(self, sql_query: str) -> str:
        comments = [
            match.group() for match in re.finditer(r"--.*$", sql_query, re.MULTILINE)
        ]
        sql_query_without_comments = re.sub(r"--.*$", "", sql_query, flags=re.MULTILINE)

        if contains_line_breaks(sql_query_without_comments.strip()):
            return sql_query

        parsed = sqlparse.format(sql_query_without_comments, reindent=True)

        return parsed + "\n" + "\n".join(comments)

    def extract_query_from_intermediate_steps(
        self, intermediate_steps: List[Tuple[AgentAction, str]]
    ) -> str:
        """Extract the SQL query from the intermediate steps."""
        sql_query = ""
        for step in intermediate_steps:
            action = step[0]
            if type(action) == AgentAction and action.tool == "SqlDbQuery":
                if "SELECT" in self.format_sql_query(action.tool_input).upper():
                    sql_query = self.remove_markdown(action.tool_input)
        if sql_query == "":
            for step in intermediate_steps:
                action = step[0]
                if "SELECT" in action.tool_input.upper():
                    sql_query = self.remove_markdown(action.tool_input)
                    if not sql_query.upper().strip().startswith("SELECT"):
                        sql_query = ""
        return sql_query

    def construct_intermediate_steps(
        self, intermediate_steps: List[Tuple[AgentAction, str]], suffix: str = ""
    ) -> List[IntermediateStep]:
        """Constructs the intermediate steps."""
        formatted_intermediate_steps = []
        for step in intermediate_steps:
            if step[0].tool == "SqlDbQuery":
                formatted_intermediate_steps.append(
                    IntermediateStep(
                        thought=str(step[0].log).split("Action:")[0],
                        action=step[0].tool,
                        action_input=step[0].tool_input,
                        observation="QUERY RESULTS ARE NOT STORED FOR PRIVACY REASONS.",
                    )
                )
            else:
                formatted_intermediate_steps.append(
                    IntermediateStep(
                        thought=str(step[0].log).split("Action:")[0],
                        action=step[0].tool,
                        action_input=step[0].tool_input,
                        observation=self.truncate_observations(step[1]),
                    )
                )
        formatted_intermediate_steps[0].thought = suffix.split("Thought: ")[1].split(
            "{agent_scratchpad}"
        )[0]
        return formatted_intermediate_steps

    def truncate_observations(self, obervarion: str, max_length: int = 2000) -> str:
        """Truncate the tool input."""
        return (
            obervarion[:max_length] + "... (truncated)"
            if len(obervarion) > max_length
            else obervarion
        )

    def filter_tables_based_on_os(self, db_scan: List[TableDescription], question: str):
        target_os_types = question.split("[OS]")[1].split("[/OS]")[0].strip().split(",")
        filtered_db_scan = []
        for table in db_scan:
            if "os_versions" in table.metadata.get("akamai", {}):
                os_versions = table.metadata["akamai"]["os_versions"]
                if any(os_version in os_versions for os_version in target_os_types):
                    filtered_db_scan.append(table)
            else:
                filtered_db_scan.append(table)
        return filtered_db_scan

    def filter_fewshot_sample_based_on_os(
        self, db_scan: List[TableDescription], fewshot_samples: List[dict]
    ):
        filtered_fewshot_samples = []
        for sample in fewshot_samples:
            target_table_names = Parser(sample["sql"]).tables
            if all(
                target_table_name in [table.table_name for table in db_scan]
                for target_table_name in target_table_names
            ):
                filtered_fewshot_samples.append(sample)
        return filtered_fewshot_samples

    @abstractmethod
    def generate_response(
        self,
        user_prompt: Prompt,
        database_connection: DatabaseConnection,
        context: List[dict] = None,
        metadata: dict = None,
    ) -> SQLGeneration:
        """Generates a response to a user question."""
        pass

    def stream_agent_steps(  # noqa: C901
        self,
        question: str,
        agent_executor: AgentExecutor,
        response: SQLGeneration,
        sql_generation_repository: SQLGenerationRepository,
        queue: Queue,
        metadata: dict = None,
    ):
        try:
            with get_openai_callback() as cb:
                for chunk in agent_executor.stream(
                    {"input": question}, {"metadata": metadata}
                ):
                    if "actions" in chunk:
                        for message in chunk["messages"]:
                            queue.put(message.content + "\n")
                    elif "steps" in chunk:
                        for step in chunk["steps"]:
                            queue.put(f"Observation: `{step.observation}`\n")
                    elif "output" in chunk:
                        queue.put(f'Final Answer: {chunk["output"]}')
                        if "```sql" in chunk["output"]:
                            response.sql = replace_unprocessable_characters(
                                self.remove_markdown(chunk["output"])
                            )
                    else:
                        raise ValueError()
        except SQLInjectionError as e:
            raise SQLInjectionError(e) from e
        except EngineTimeOutORItemLimitError as e:
            raise EngineTimeOutORItemLimitError(e) from e
        except Exception as e:
            response.sql = ("",)
            response.status = ("INVALID",)
            response.error = (str(e),)
        finally:
            queue.put(None)
            response.tokens_used = cb.total_tokens
            response.completed_at = datetime.datetime.now()
            if not response.error:
                if response.sql:
                    response = self.create_sql_query_status(
                        self.database,
                        response.sql,
                        response,
                    )
                else:
                    response.status = "INVALID"
                    response.error = "No SQL query generated"
            sql_generation_repository.update(response)

    @abstractmethod
    def stream_response(
        self,
        user_prompt: Prompt,
        database_connection: DatabaseConnection,
        response: SQLGeneration,
        queue: Queue,
        metadata: dict = None,
    ):
        """Streams a response to a user question."""
        pass
