import logging
import warnings

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logging.getLogger().setLevel(logging.ERROR)
warnings.filterwarnings("ignore")

console = Console()


def print_event(message):
    def print_message_header(label, color):
        box_width = 112
        padding = (box_width - len(label)) // 2
        text = f"\n[bold {color}]{'-' * padding} {label} {'-' * (box_width - len(label) - padding)}[/bold {color}]"
        console.print(text)

    def format_tool_calls(tool_calls):
        # Create a table for displaying tools and arguments
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()

        for call in tool_calls:
            tool_name = call.get("name", "Unknown Tool")
            args = call.get("args", {})

            # Add the tool name as a header
            table.add_row(f"[bold]Tool[/bold]: {tool_name}", "")
            table.add_row("[bold]Arguments:[/bold]", "")

            # Add each argument as a row under the tool
            for key, value in args.items():
                formatted_value = Text(str(value))
                table.add_row(f"  • {key}:", formatted_value)

        console.print(Panel(table, title="Tool Calls", expand=False))

    if isinstance(message, SystemMessage):
        print_message_header("System Message", "blue")
        console.print(Markdown(message.content))

    if isinstance(message, HumanMessage):
        print_message_header("Human Message", "green")
        console.print(Markdown(message.content))

    elif isinstance(message, AIMessage):
        print_message_header("AI Message", "magenta")
        if message.content:
            if isinstance(message.content, list):
                for item in message.content:
                    console.print(Markdown(item["text"]))
            else:
                console.print(Markdown(message.content))

        # Handle tool calls if they exist
        if message.tool_calls:
            format_tool_calls(message.tool_calls)

    elif isinstance(message, ToolMessage):
        print_message_header("Tool Message", "red")
        console.print(f"[bold]Tool Name: [/bold]{message.name}")
        console.print("[bold]Tool Result:[/bold]")
        console.print(Markdown(message.content))


def print_header(label, color):
    box_width = 112
    padding = (box_width - len(label)) // 2
    text = f"{' ' * padding}{label}{' ' * (box_width - len(label) - padding)}"
    panel = Panel(text, expand=False, style=color)
    console.print(panel)
