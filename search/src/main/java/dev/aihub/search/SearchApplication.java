package dev.aihub.search;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

import dev.aihub.search.http.LocalSearchServer;

@SpringBootApplication
public class SearchApplication {

	public static void main(String[] args) {
		var context = SpringApplication.run(SearchApplication.class, args);
		if (!context.getBean(LocalSearchServer.class).isRunning()) {
			System.exit(SpringApplication.exit(context));
		}
	}
}
