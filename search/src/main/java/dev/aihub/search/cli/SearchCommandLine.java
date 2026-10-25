package dev.aihub.search.cli;

import java.io.PrintWriter;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.ExitCodeGenerator;
import org.springframework.stereotype.Component;

@Component
public class SearchCommandLine implements ApplicationRunner, ExitCodeGenerator {

	private final CommandRouter router;
	private int exitCode;

	public SearchCommandLine(CommandRouter router) {
		this.router = router;
	}

	@Override
	public void run(ApplicationArguments args) {
		exitCode = router.run(args.getSourceArgs(), new PrintWriter(System.out), new PrintWriter(System.err));
	}

	@Override
	public int getExitCode() {
		return exitCode;
	}
}
